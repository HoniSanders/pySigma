from abc import ABC, abstractmethod
from functools import partial
from sigma.conditions import ConditionOR, SigmaCondition
from typing import (
    Any,
    Iterable,
    List,
    Dict,
    Literal,
    Optional,
    Set,
    Union,
    Pattern,
    Iterator,
    get_args,
    get_origin,
    Callable,
)
from dataclasses import dataclass, field
import dataclasses
import random
import string
import re
import sigma
from sigma.correlations import SigmaCorrelationRule
from sigma.rule import SigmaLogSource, SigmaRule, SigmaDetection, SigmaDetectionItem, SigmaRuleBase
from sigma.exceptions import (
    SigmaRegularExpressionError,
    SigmaTransformationError,
    SigmaValueError,
    SigmaConfigurationError,
)
from sigma.types import (
    Placeholder,
    SigmaBool,
    SigmaNull,
    SigmaNumber,
    SigmaRegularExpression,
    SigmaRegularExpressionFlag,
    SigmaString,
    SigmaType,
    SpecialChars,
    SigmaQueryExpression,
    SigmaFieldReference,
)


### Base Classes ###
@dataclass
class Transformation(ABC):
    """
    Base class for processing steps used in pipelines. Override `apply` with transformation that is
    applied to the whole rule.
    """

    processing_item: Optional["sigma.processing.pipeline.ProcessingItem"] = field(
        init=False, compare=False, default=None
    )

    @classmethod
    def from_dict(cls, d: dict) -> "Transformation":
        try:
            return cls(**d)
        except TypeError as e:
            raise SigmaConfigurationError("Error in instantiation of transformation: " + str(e))

    @abstractmethod
    def apply(
        self,
        pipeline: "sigma.processing.pipeline.ProcessingPipeline",
        rule: Union[SigmaRule, SigmaCorrelationRule],
    ) -> None:
        """Apply transformation on Sigma rule."""
        self._pipeline: "sigma.processing.pipeline.ProcessingPipeline" = (
            pipeline  # make pipeline accessible from all further options in class property
        )
        self.processing_item_applied(rule)

    def set_processing_item(self, processing_item: "sigma.processing.pipeline.ProcessingItem"):
        self.processing_item = processing_item

    def processing_item_applied(
        self,
        d: Union[
            SigmaRule, SigmaDetection, SigmaDetectionItem, SigmaCondition, SigmaCorrelationRule
        ],
    ):
        """Mark detection item or detection as applied."""
        d.add_applied_processing_item(self.processing_item)


@dataclass
class DetectionItemTransformation(Transformation):
    """
    Iterates over all detection items of a Sigma rule and calls the apply_detection_item method
    for each of them if the detection item condition associated with the processing item evaluates
    to true. It also takes care to recurse into detections nested into detections.

    The apply_detection_item method can directly change the detection or return a replacement
    object, which can be a SigmaDetection or a SigmaDetectionItem.

    The processing item is automatically added to the applied items of the detection items if a
    replacement value was returned. In the other case the apply_detection_item method must take
    care of this to make conditional decisions in the processing pipeline working. This can be
    done with the detection_item_applied() method.

    A detection item transformation also marks the item as unconvertible to plain data types.
    """

    @abstractmethod
    def apply_detection_item(
        self, detection_item: SigmaDetectionItem
    ) -> Optional[Union[SigmaDetection, SigmaDetectionItem]]:
        """Apply transformation on detection item."""

    def apply_detection(self, detection: SigmaDetection):
        for i, detection_item in enumerate(detection.detection_items):
            if isinstance(detection_item, SigmaDetection):  # recurse into nested detection items
                self.apply_detection(detection_item)
            else:
                if (
                    self.processing_item is None
                    or self.processing_item.match_detection_item(self._pipeline, detection_item)
                ) and (r := self.apply_detection_item(detection_item)) is not None:
                    if isinstance(r, SigmaDetectionItem):
                        r.disable_conversion_to_plain()
                    detection.detection_items[i] = r
                    self.processing_item_applied(r)

    def apply(
        self, pipeline: "sigma.processing.pipeline.ProcessingPipeline", rule: SigmaRule
    ) -> None:
        super().apply(pipeline, rule)
        if isinstance(rule, SigmaRule):
            for detection in rule.detection.detections.values():
                self.apply_detection(detection)


@dataclass
class FieldMappingTransformationBase(DetectionItemTransformation):
    """
    Transformation that is applied to detection items and additionally the field list of a Sigma
    rule.
    """

    @abstractmethod
    def apply_field_name(self, field: str) -> List[str]:
        """
        Apply field name transformation to a field list item of a Sigma rule. It must always return
        a list of strings that are expanded into a new field list.
        """

    def _apply_field_name(
        self, pipeline: "sigma.processing.pipeline.ProcessingPipeline", field: str
    ) -> List[str]:
        """
        Evaluate field name conditions and perform transformation with apply_field_name() method if
        condition matches, else return original value.
        """
        if self.processing_item is None or self.processing_item.match_field_name(pipeline, field):
            result = self.apply_field_name(field)
            if self.processing_item is not None:
                pipeline.track_field_processing_items(
                    field, result, self.processing_item.identifier
                )
            return result
        else:
            return [field]

    def apply(
        self,
        pipeline: "sigma.processing.pipeline.ProcessingPipeline",
        rule: Union[SigmaRule, SigmaCorrelationRule],
    ) -> None:
        """Apply field name transformations to Sigma rule field names listed in 'fields' attribute."""
        _apply_field_name = partial(self._apply_field_name, pipeline)
        rule.fields = [item for mapping in map(_apply_field_name, rule.fields) for item in mapping]
        if isinstance(rule, SigmaCorrelationRule):
            if rule.group_by is not None:
                # first iterate over aliases, map the field names contained in them and keep track
                # of aliases used later in grouping list and shouldn't be mapped.
                aliases = set()
                for alias in rule.aliases:
                    aliases.add(alias.alias)
                    for rule_reference, field_name in alias.mapping.items():
                        mapped_field_name = _apply_field_name(field_name)
                        if len(mapped_field_name) > 1:
                            raise SigmaConfigurationError(
                                "Field name mapping transformation can't be applied to correlation rule alias mapping because it results in multiple field names."
                            )
                        alias.mapping[rule_reference] = mapped_field_name[0]

                # now iterate over grouping list and map field names if not contained in aliases
                rule.group_by = [
                    item
                    for field_name in rule.group_by
                    for item in (
                        _apply_field_name(field_name) if field_name not in aliases else [field_name]
                    )
                ]

            # finally map the field name in the condition
            if rule.condition is not None and (fieldref := rule.condition.fieldref) is not None:
                mapped_field = _apply_field_name(fieldref)
                if len(mapped_field) > 1:
                    raise SigmaConfigurationError(
                        "Field name mapping transformation can't be applied to correlation rule condition field reference because it results in multiple field names."
                    )
                rule.condition.fieldref = mapped_field[0]

        return super().apply(pipeline, rule)

    def apply_detection_item(
        self, detection_item: SigmaDetectionItem
    ) -> Optional[Union[SigmaDetection, SigmaDetectionItem]]:
        """Apply field name transformations to field references in detection item values."""
        new_values = []
        match = False
        for value in detection_item.value:
            if self.processing_item is not None and self.processing_item.match_field_in_value(
                self._pipeline, value
            ):
                new_values.extend(
                    (
                        SigmaFieldReference(mapped_field)
                        for mapped_field in self._apply_field_name(self._pipeline, value.field)
                    )
                )
                match = True
            else:
                new_values.append(value)

        if match:  # replace value only if something matched
            detection_item.value = new_values

        return super().apply_detection_item(detection_item)


@dataclass
class ValueTransformation(DetectionItemTransformation):
    """
    Iterates over all values in all detection items of a Sigma rule and call apply_value method
    for each of them. The apply_value method can return a single value or a list of values which
    are inserted into the value list or None if the original value should be passed through. An
    empty list should be returned by apply_value to drop the value from the transformed results.
    """

    def __post_init__(self):
        argtypes = list(
            self.apply_value.__annotations__.values()
        )  # get type annotations of apply_value method
        try:  # try to extract type annotation of first argument and derive accepted types
            argtype = argtypes[1]
            if (
                hasattr(argtype, "__origin__") and argtype.__origin__ is Union
            ):  # if annotation is an union the list of types is contained in __args__
                self.value_types = argtype.__args__
            else:
                self.value_types = argtype
        except IndexError:  # No type annotation found
            self.value_types = None

    def apply_detection_item(self, detection_item: SigmaDetectionItem):
        """Call apply_value for each value and integrate results into value list."""
        results = []
        modified = False
        for value in detection_item.value:
            if self.value_types is None or isinstance(
                value, self.value_types
            ):  # run replacement if no type annotation is defined or matching to type of value
                res = self.apply_value(detection_item.field, value)
                if res is None:  # no value returned: drop value
                    results.append(value)
                elif isinstance(res, Iterable) and not isinstance(res, SigmaType):
                    results.extend(res)
                    modified = True
                else:
                    results.append(res)
                    modified = True
            else:  # pass original value if type doesn't matches to apply_value argument type annotation
                results.append(value)
        if modified:
            detection_item.value = results
            self.processing_item_applied(detection_item)

    @abstractmethod
    def apply_value(
        self, field: str, val: SigmaType
    ) -> Optional[Union[SigmaType, Iterable[SigmaType]]]:
        """
        Perform a value transformation. This method can return:

        * None to drop the value
        * a single SigmaType object which replaces the original value.
        * an iterable of SigmaType objects. These objects are used as replacement for the
          original value.

        The type annotation of the val argument is used to skip incompatible values.
        """


class StringValueTransformation(ValueTransformation):
    """
    Base class for transformations that operate on SigmaString values.
    """

    def apply_value(self, field: str, val: SigmaString) -> Optional[SigmaString]:
        if isinstance(val, SigmaString):
            return self.apply_string_value(field, val)

    @abstractmethod
    def apply_string_value(self, field: str, val: SigmaString) -> Optional[SigmaString]:
        """
        Perform a value transformation. This method can return:

        * None to drop the value
        * a single SigmaString object which replaces the original value.
        """


@dataclass
class ConditionTransformation(Transformation):
    """
    Iterates over all rule conditions and calls the apply_condition method for each condition. Automatically
    takes care of marking condition as applied by processing item.
    """

    def apply(
        self, pipeline: "sigma.processing.pipeline.ProcessingPipeline", rule: SigmaRule
    ) -> None:
        super().apply(pipeline, rule)
        if isinstance(rule, SigmaRule):
            for i, condition in enumerate(rule.detection.parsed_condition):
                condition_before = condition.condition
                self.apply_condition(condition)
                if (
                    condition.condition != condition_before
                ):  # Condition was changed by transformation,
                    self.processing_item_applied(
                        condition
                    )  # mark as processed by processing item containing this transformation

    @abstractmethod
    def apply_condition(self, cond: SigmaCondition) -> None:
        """
        This method is invoked for each condition and can change it.
        """


### Transformations ###
@dataclass
class FieldMappingTransformation(FieldMappingTransformationBase):
    """Map a field name to one or multiple different."""

    mapping: Dict[str, Union[str, List[str]]]

    def get_mapping(self, field: str) -> Union[None, str, List[str]]:
        return self.mapping.get(field)

    def apply_detection_item(self, detection_item: SigmaDetectionItem):
        super().apply_detection_item(detection_item)
        field = detection_item.field
        mapping = self.get_mapping(field)
        if mapping is not None and self.processing_item.match_field_name(self._pipeline, field):
            self._pipeline.field_mappings.add_mapping(field, mapping)
            if isinstance(mapping, str):  # 1:1 mapping, map field name of detection item directly
                detection_item.field = mapping
                self.processing_item_applied(detection_item)
            else:
                return SigmaDetection(
                    [
                        dataclasses.replace(detection_item, field=field, auto_modifiers=False)
                        for field in mapping
                    ],
                    item_linking=ConditionOR,
                )

    def apply_field_name(self, field: str) -> Union[str, List[str]]:
        mapping = self.get_mapping(field) or field
        if isinstance(mapping, str):
            return [mapping]
        else:
            return mapping


@dataclass
class FieldPrefixMappingTransformation(FieldMappingTransformation):
    """Map a field name prefix to one or multiple different prefixes."""

    def get_mapping(self, field: str) -> Union[None, str, List[str]]:
        for src, dest in self.mapping.items():
            if field.startswith(src):  # found matching prefix
                if isinstance(dest, str):
                    return dest + field[len(src) :]
                else:
                    return [dest_item + field[len(src) :] for dest_item in dest]


@dataclass
class FieldFunctionTransformation(FieldMappingTransformationBase):
    """Map a field name to another using provided transformation function.
    You can overwrite transformation by providing explicit mapping for a field."""

    transform_func: Callable[[str], str]
    mapping: Dict[str, str] = field(default_factory=lambda: {})

    def _transform_name(self, f: str) -> str:
        if f:
            return self.mapping.get(f, self.transform_func(f))
        return f

    def apply_detection_item(self, detection_item: SigmaDetectionItem):
        super().apply_detection_item(detection_item)
        f = detection_item.field
        mapping = self._transform_name(f)
        if self.processing_item.match_field_name(self._pipeline, f):
            self._pipeline.field_mappings.add_mapping(f, mapping)
            detection_item.field = mapping
            self.processing_item_applied(detection_item)

    def apply_field_name(self, f: str) -> Union[str, List[str]]:
        return [self._transform_name(f)]


@dataclass
class DropDetectionItemTransformation(DetectionItemTransformation):
    """Deletes detection items. This should only used in combination with a detection item
    condition."""

    class DeleteSigmaDetectionItem(SigmaDetectionItem):
        """Class is used to mark detection item as to be deleted. It's just for having all the
        detection item functionality available."""

        @classmethod
        def create(cls):
            return cls(None, [], [])

    def apply_detection_item(
        self, detection_item: SigmaDetectionItem
    ) -> Optional[SigmaDetectionItem]:
        """This function only marks detection items for deletion."""
        return self.DeleteSigmaDetectionItem.create()

    def apply_detection(self, detection: SigmaDetection):
        super().apply_detection(detection)
        detection.detection_items = list(
            filter(
                lambda d: not isinstance(d, self.DeleteSigmaDetectionItem),
                detection.detection_items,
            )
        )


@dataclass
class AddFieldnameSuffixTransformation(FieldMappingTransformationBase):
    """
    Add field name suffix.
    """

    suffix: str

    def apply_detection_item(self, detection_item: SigmaDetectionItem):
        super().apply_detection_item(detection_item)
        if type(orig_field := detection_item.field) is str and (
            self.processing_item is None
            or self.processing_item.match_field_name(self._pipeline, orig_field)
        ):
            detection_item.field += self.suffix
            self._pipeline.field_mappings.add_mapping(orig_field, detection_item.field)
        self.processing_item_applied(detection_item)

    def apply_field_name(self, field: str) -> List[str]:
        return [field + self.suffix]


@dataclass
class AddFieldnamePrefixTransformation(FieldMappingTransformationBase):
    """
    Add field name prefix.
    """

    prefix: str

    def apply_detection_item(self, detection_item: SigmaDetectionItem):
        super().apply_detection_item(detection_item)
        if type(orig_field := detection_item.field) is str and (
            self.processing_item is None
            or self.processing_item.match_field_name(self._pipeline, orig_field)
        ):
            detection_item.field = self.prefix + detection_item.field
            self._pipeline.field_mappings.add_mapping(orig_field, detection_item.field)
        self.processing_item_applied(detection_item)

    def apply_field_name(self, field: str) -> List[str]:
        return [self.prefix + field]


@dataclass
class PlaceholderIncludeExcludeMixin:
    include: Optional[List[str]] = field(default=None)
    exclude: Optional[List[str]] = field(default=None)

    def __post_init__(self):
        super().__post_init__()
        if self.include is not None and self.exclude is not None:
            raise SigmaConfigurationError(
                "Placeholder transformation include and exclude lists can only be used exclusively!"
            )

    def is_handled_placeholder(self, p: Placeholder) -> bool:
        return (
            (self.include is None and self.exclude is None)
            or (self.include is not None and p.name in self.include)
            or (self.exclude is not None and p.name not in self.exclude)
        )


@dataclass
class BasePlaceholderTransformation(PlaceholderIncludeExcludeMixin, ValueTransformation):
    """
    Placeholder base transformation. The parameters include and exclude can contain variable names that
    are handled by this transformation. Unhandled placeholders are left as they are and must be handled by
    later transformations.
    """

    def __post_init__(self):
        super().__post_init__()

    def apply_value(
        self, field: str, val: SigmaString
    ) -> Union[SigmaString, Iterable[SigmaString]]:
        if val.contains_placeholder(self.include, self.exclude):
            return val.replace_placeholders(self.placeholder_replacements_base)
        else:
            return None

    def placeholder_replacements_base(
        self, p: Placeholder
    ) -> Iterator[Union[str, SpecialChars, Placeholder]]:
        """
        Base placeholder replacement callback. Calls real callback if placeholder is included or not excluded,
        else it passes the placeholder back to caller.
        """
        if self.is_handled_placeholder(p):
            yield from self.placeholder_replacements(p)
        else:
            yield p

    @abstractmethod
    def placeholder_replacements(
        self, p: Placeholder
    ) -> Iterator[Union[str, SpecialChars, Placeholder]]:
        """
        Placeholder replacement callback used by SigmaString.replace_placeholders(). This must return one
        of the following object types:

        * Plain strings
        * SpecialChars instances for insertion of wildcards
        * Placeholder instances, it may even return the same placeholder. These must be handled by following processing
          pipeline items or the backend or the conversion will fail.
        """


@dataclass
class WildcardPlaceholderTransformation(BasePlaceholderTransformation):
    """
    Replaces placeholders with wildcards. This transformation is useful if remaining placeholders should
    be replaced with something meaningful to make conversion of rules possible without defining the
    placeholders content.
    """

    def placeholder_replacements(self, p: Placeholder) -> Iterator[SpecialChars]:
        return [SpecialChars.WILDCARD_MULTI]


@dataclass
class ValueListPlaceholderTransformation(BasePlaceholderTransformation):
    """
    Replaces placeholders with values contained in variables defined in the configuration.
    """

    def placeholder_replacements(self, p: Placeholder) -> List[str]:
        try:
            values = self._pipeline.vars[p.name]
        except KeyError:
            raise SigmaValueError(f"Placeholder replacement variable '{ p.name }' doesn't exists.")

        if not isinstance(values, List):
            values = [values]

        if {isinstance(item, (str, int, float)) for item in values} != {True}:
            raise SigmaValueError(
                f"Replacement variable '{ p.name }' contains value which is not a string or number."
            )

        return [SigmaString(str(v)) for v in values]


@dataclass
class QueryExpressionPlaceholderTransformation(PlaceholderIncludeExcludeMixin, ValueTransformation):
    """
    Replaces a placeholder with a plain query containing the placeholder or an identifier
    mapped from the placeholder name. The main purpose is the generation of arbitrary
    list lookup expressions which are passed to the resulting query.

    Parameters:
    * expression: string that contains query expression with {field} and {id} placeholder
    where placeholder identifier or a mapped identifier is inserted.
    * mapping: Mapping between placeholders and identifiers that should be used in the expression.
    If no mapping is provided the placeholder name is used.
    """

    expression: str = ""
    mapping: Dict[str, str] = field(default_factory=dict)

    def apply_value(
        self, field: str, val: SigmaString
    ) -> Union[SigmaString, Iterable[SigmaString]]:
        if val.contains_placeholder():
            if len(val.s) == 1:  # Sigma string must only contain placeholder, nothing else.
                p = val.s[0]
                if self.is_handled_placeholder(p):
                    return SigmaQueryExpression(self.expression, self.mapping.get(p.name) or p.name)
            else:  # SigmaString contains placeholder as well as other parts
                raise SigmaValueError(
                    f"Placeholder query expression transformation only allows placeholder-only strings."
                )
        return None


@dataclass
class AddConditionTransformation(ConditionTransformation):
    """
    Add a condition expression to rule conditions.

    If template is set to True the condition values are interpreted as string templates and the
    following placeholders are replaced:

    * $category, $product and $service: with the corresponding values of the Sigma rule log source.
    """

    conditions: Dict[str, Union[str, List[str]]] = field(default_factory=dict)
    name: Optional[str] = field(default=None, compare=False)
    template: bool = False
    negated: bool = False

    def __post_init__(self):
        if self.name is None:  # generate random detection item name if none is given
            self.name = "_cond_" + ("".join(random.choices(string.ascii_lowercase, k=10)))

    def apply(
        self, pipeline: "sigma.processing.pipeline.ProcessingPipeline", rule: SigmaRule
    ) -> None:
        if isinstance(rule, SigmaRule):
            if self.template:
                conditions = {
                    field: (
                        [
                            string.Template(item).safe_substitute(
                                category=rule.logsource.category,
                                product=rule.logsource.product,
                                service=rule.logsource.service,
                            )
                            for item in value
                        ]
                        if isinstance(value, list)
                        else string.Template(value).safe_substitute(
                            category=rule.logsource.category,
                            product=rule.logsource.product,
                            service=rule.logsource.service,
                        )
                    )
                    for field, value in self.conditions.items()
                }
            else:
                conditions = self.conditions

            rule.detection.detections[self.name] = SigmaDetection.from_definition(conditions)
            self.processing_item_applied(rule.detection.detections[self.name])
            super().apply(pipeline, rule)

    def apply_condition(self, cond: SigmaCondition) -> None:
        cond.condition = ("not " if self.negated else "") + f"{self.name} and ({cond.condition})"


@dataclass
class ChangeLogsourceTransformation(Transformation):
    """Replace log source as defined in transformation parameters."""

    category: Optional[str] = field(default=None)
    product: Optional[str] = field(default=None)
    service: Optional[str] = field(default=None)

    def apply(
        self, pipeline: "sigma.processing.pipeline.ProcessingPipeline", rule: SigmaRule
    ) -> None:
        super().apply(pipeline, rule)
        logsource = SigmaLogSource(self.category, self.product, self.service)
        rule.logsource = logsource


@dataclass
class AddFieldTransformation(Transformation):
    """
    Add one or multiple fields to the Sigma rule. The field is added to the fields list of the rule:
    """

    field: Union[str, List[str]]

    def apply(
        self, pipeline: "sigma.processing.pipeline.ProcessingPipeline", rule: SigmaRule
    ) -> None:
        super().apply(pipeline, rule)
        if isinstance(self.field, str):
            rule.fields.append(self.field)
        elif isinstance(self.field, list):
            rule.fields.extend(self.field)


@dataclass
class RemoveFieldTransformation(Transformation):
    """
    Remove one or multiple fields from the Sigma rules field list. If a given field is not in the
    rules list, it is ignored.
    """

    field: Union[str, List[str]]

    def apply(
        self, pipeline: "sigma.processing.pipeline.ProcessingPipeline", rule: SigmaRule
    ) -> None:
        super().apply(pipeline, rule)
        if isinstance(self.field, str):
            try:
                rule.fields.remove(self.field)
            except ValueError:
                pass
        elif isinstance(self.field, list):
            for field in self.field:
                try:
                    rule.fields.remove(field)
                except ValueError:
                    pass


@dataclass
class SetFieldTransformation(Transformation):
    """
    Set fields to the Sigma rule. The fields are set to the fields list of the transformation.
    """

    fields: List[str]

    def apply(
        self, pipeline: "sigma.processing.pipeline.ProcessingPipeline", rule: SigmaRule
    ) -> None:
        super().apply(pipeline, rule)
        rule.fields = self.fields


@dataclass
class ReplaceStringTransformation(StringValueTransformation):
    """
    Replace string part matched by regular expresssion with replacement string that can reference
    capture groups. It operates on the plain string parts of the SigmaString value.

    This is basically an interface to re.sub() and can use all features available there.
    """

    regex: str
    replacement: str

    def __post_init__(self):
        super().__post_init__()
        try:
            self.re = re.compile(self.regex)
        except re.error as e:
            raise SigmaRegularExpressionError(
                f"Regular expression '{self.regex}' is invalid: {str(e)}"
            ) from e

    def apply_string_value(self, field: str, val: SigmaString) -> SigmaString:
        if isinstance(val, SigmaString):
            return val.map_parts(
                lambda s: self.re.sub(self.replacement, s), lambda p: isinstance(p, str)
            )


@dataclass
class MapStringTransformation(StringValueTransformation):
    """
    Map static string value to one or multiple other strings.
    """

    mapping: Dict[str, Union[str, List[str]]]

    def apply_string_value(self, field: str, val: SigmaString) -> Optional[SigmaString]:
        mapped = self.mapping.get(str(val), None)
        if isinstance(mapped, str):
            return SigmaString(mapped)
        elif isinstance(mapped, list):
            return [SigmaString(item) for item in mapped]


@dataclass
class RegexTransformation(StringValueTransformation):
    """
    Transform a string value to a case insensitive regular expression. The following methods are
    available and can be selected with the method parameter:

    * plain: Convert the string to a regular expression without any change to its case. In most
      cases this should result in a case-sensitive match of the string.
    * case_insensitive_flag: Add the case insensitive flag to the regular expression.
    * case_insensitive_brackets (default): Wrap each character in a bracket expression like [aA] to match
      both case variants.

    This transformation is intended to be used to emulate case insensitive matching in backends that
    don't support it natively.
    """

    method: Literal["plain", "ignore_case_flag", "ignore_case_brackets"] = "ignore_case_brackets"

    def __post_init__(self):
        if self.method not in self.__annotations__["method"].__args__:
            raise SigmaConfigurationError(
                f"Invalid method '{self.method}' for CaseInsensitiveRegexTransformation."
            )
        return super().__post_init__()

    def apply_string_value(self, field: str, val: SigmaString) -> Optional[SigmaString]:
        regex = ""
        for sc in val.s:  # iterate over all SigmaString components (strings and special chars)
            if isinstance(sc, str):  # if component is a string
                if (
                    self.method == "ignore_case_brackets"
                ):  # wrap each character in a bracket expression
                    regex += "".join(
                        f"[{c.lower()}{c.upper()}]" if c.isalpha() else re.escape(c) for c in sc
                    )
                else:
                    regex += re.escape(sc)
            elif (
                sc == SpecialChars.WILDCARD_MULTI
            ):  # if component is a wildcard, add it as regex .*
                regex += ".*"
            elif (
                sc == SpecialChars.WILDCARD_SINGLE
            ):  # if component is a single wildcard, add it as regex .
                regex += "."
            elif isinstance(sc, Placeholder):  # Placeholders are not allowed in regex
                raise SigmaConfigurationError(
                    f"Placeholder '{sc.name}' can't be converted to a regular expression. Please use a placeholder resolution transformation before."
                )
        if self.method == "ignore_case_flag":
            return SigmaRegularExpression(regex, {SigmaRegularExpressionFlag.IGNORECASE})
        else:
            return SigmaRegularExpression(regex)


@dataclass
class SetValueTransformation(ValueTransformation):
    """
    Set value to a fixed value. The type of the value can be enforced to `str` or `num` with the
    force_type parameter.
    """

    value: Optional[Union[str, int, float, bool]]
    force_type: Optional[Literal["str", "num"]] = None

    def __post_init__(self):
        if self.force_type is None:  # no type forced, use type of value
            if isinstance(self.value, str):
                self.sigma_value = SigmaString(self.value)
            elif isinstance(self.value, bool):
                self.sigma_value = SigmaBool(self.value)
            elif isinstance(self.value, (int, float)):
                self.sigma_value = SigmaNumber(self.value)
            elif self.value is None:
                self.sigma_value = SigmaNull()
            else:
                raise SigmaConfigurationError(
                    f"Unsupported value type '{type(self.value)} for {str(self)}'"
                )
        else:  # forced type
            if not isinstance(self.value, (str, int, float)):  # only allowed for certain types
                raise SigmaConfigurationError(
                    f"force_type '{self.force_type}' is only allowed for string and numeric values"
                )
            if self.force_type == "str":
                self.sigma_value = SigmaString(str(self.value))
            elif self.force_type == "num":
                try:
                    self.sigma_value = SigmaNumber(self.value)
                except SigmaValueError:
                    raise SigmaConfigurationError(
                        f"Value '{self.value}' can't be converted to number for {str(self)}"
                    )
            else:
                raise SigmaConfigurationError(
                    f"Invalid force_type '{self.force_type}' for {str(self)}"
                )

        return super().__post_init__()

    def apply_value(self, field: str, val: SigmaType) -> SigmaType:
        return self.sigma_value


@dataclass
class ConvertTypeTransformation(ValueTransformation):
    """
    Convert type of value. The conversion into strings and numbers is currently supported.
    """

    target_type: Literal["str", "num"]

    def apply_value(self, field: str, val: SigmaType) -> Optional[Union[SigmaString, SigmaNumber]]:
        if self.target_type == "str":
            return SigmaString(str(val))
        elif self.target_type == "num":
            try:
                return SigmaNumber(str(val))
            except SigmaValueError:
                raise SigmaValueError(f"Value '{val}' can't be converted to number for {str(self)}")


@dataclass
class SetStateTransformation(Transformation):
    """Set pipeline state key to value."""

    key: str
    val: Any

    def apply(self, pipeline: "sigma.processing.pipeline.Proces", rule: SigmaRule) -> None:
        super().apply(pipeline, rule)
        pipeline.state[self.key] = self.val


@dataclass
class RuleFailureTransformation(Transformation):
    """
    Raise a SigmaTransformationError with the provided message. This enables transformation
    pipelines to signalize that a certain situation can't be handled, e.g. only a subset of values
    is allowed because the target data model doesn't offers all possibilities.
    """

    message: str

    def apply(
        self, pipeline: "sigma.processing.pipeline.ProcessingPipeline", rule: SigmaRule
    ) -> None:
        raise SigmaTransformationError(self.message)


@dataclass
class DetectionItemFailureTransformation(DetectionItemTransformation):
    """
    Raise a SigmaTransformationError with the provided message. This enables transformation
    pipelines to signalize that a certain situation can't be handled, e.g. only a subset of values
    is allowed because the target data model doesn't offers all possibilities.
    """

    message: str

    def apply_detection_item(self, detection_item: SigmaDetectionItem) -> None:
        raise SigmaTransformationError(self.message)


@dataclass
class SetCustomAttributeTransformation(Transformation):
    """
    Sets an arbitrary custom attribute on a rule, that can be used by a backend during processing.
    """

    attribute: str
    value: Any

    def apply(
        self,
        pipeline: "sigma.processing.pipeline.ProcessingPipeline",
        rule: Union[SigmaRule, SigmaCorrelationRule],
    ) -> None:
        super().apply(pipeline, rule)
        rule.custom_attributes[self.attribute] = self.value


@dataclass
class NestedProcessingTransformation(Transformation):
    """Executes a nested processing pipeline as transformation. Main purpose is to apply a
    whole set of transformations that match the given conditions of the enclosng processing item.
    """

    items: List["sigma.processing.pipeline.ProcessingItem"]
    _nested_pipeline: "sigma.processing.pipeline.ProcessingPipeline" = field(
        init=False, compare=False, repr=False
    )

    def __post_init__(self):
        from sigma.processing.pipeline import (
            ProcessingPipeline,
        )  # TODO: move to top-level after restructuring code

        self._nested_pipeline = ProcessingPipeline(items=self.items)

    @classmethod
    def from_dict(cls, d: Dict) -> "NestedProcessingTransformation":
        from sigma.processing.pipeline import (
            ProcessingItem,
        )  # TODO: move to top-level after restructuring code

        try:
            return cls(items=[ProcessingItem.from_dict(item) for item in d["items"]])
        except KeyError:
            raise SigmaConfigurationError(
                "Nested processing transformation requires an 'items' key."
            )

    def apply(
        self,
        pipeline: "sigma.processing.pipeline.ProcessingPipeline",
        rule: Union[SigmaRule, SigmaCorrelationRule],
    ) -> None:
        super().apply(pipeline, rule)
        self._nested_pipeline.apply(rule)
        pipeline.applied.extend(self._nested_pipeline.applied)
        pipeline.applied_ids.update(self._nested_pipeline.applied_ids)
        pipeline.field_name_applied_ids.update(self._nested_pipeline.field_name_applied_ids)
        pipeline.field_mappings.merge(self._nested_pipeline.field_mappings)
        pipeline.state.update(self._nested_pipeline.state)


transformations: Dict[str, Transformation] = {
    "field_name_mapping": FieldMappingTransformation,
    "field_name_prefix_mapping": FieldPrefixMappingTransformation,
    "field_name_transform": FieldFunctionTransformation,
    "drop_detection_item": DropDetectionItemTransformation,
    "field_name_suffix": AddFieldnameSuffixTransformation,
    "field_name_prefix": AddFieldnamePrefixTransformation,
    "wildcard_placeholders": WildcardPlaceholderTransformation,
    "value_placeholders": ValueListPlaceholderTransformation,
    "query_expression_placeholders": QueryExpressionPlaceholderTransformation,
    "add_condition": AddConditionTransformation,
    "change_logsource": ChangeLogsourceTransformation,
    "add_field": AddFieldTransformation,
    "remove_field": RemoveFieldTransformation,
    "set_field": SetFieldTransformation,
    "replace_string": ReplaceStringTransformation,
    "map_string": MapStringTransformation,
    "set_state": SetStateTransformation,
    "regex": RegexTransformation,
    "set_value": SetValueTransformation,
    "convert_type": ConvertTypeTransformation,
    "rule_failure": RuleFailureTransformation,
    "detection_item_failure": DetectionItemFailureTransformation,
    "set_custom_attribute": SetCustomAttributeTransformation,
    "nest": NestedProcessingTransformation,
}
