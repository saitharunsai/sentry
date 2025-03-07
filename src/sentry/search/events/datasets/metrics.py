from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping, Optional, Union

import sentry_sdk
from django.utils.functional import cached_property
from snuba_sdk import Column, Condition, Function, Op

from sentry.api.event_search import SearchFilter
from sentry.exceptions import IncompatibleMetricsQuery, InvalidSearchQuery
from sentry.models.transaction_threshold import (
    TRANSACTION_METRICS,
    ProjectTransactionThreshold,
    ProjectTransactionThresholdOverride,
)
from sentry.search.events import constants, fields
from sentry.search.events.builder import MetricsQueryBuilder
from sentry.search.events.datasets import field_aliases, filter_aliases
from sentry.search.events.datasets.base import DatasetConfig
from sentry.search.events.types import SelectType, WhereType
from sentry.utils.numbers import format_grouped_length
from sentry.utils.snuba import is_duration_measurement, is_span_op_breakdown


class MetricsDatasetConfig(DatasetConfig):
    missing_function_error = IncompatibleMetricsQuery

    def __init__(self, builder: MetricsQueryBuilder):
        self.builder = builder

    @property
    def search_filter_converter(
        self,
    ) -> Mapping[str, Callable[[SearchFilter], Optional[WhereType]]]:
        return {
            constants.PROJECT_ALIAS: self._project_slug_filter_converter,
            constants.PROJECT_NAME_ALIAS: self._project_slug_filter_converter,
            constants.EVENT_TYPE_ALIAS: self._event_type_converter,
            constants.TEAM_KEY_TRANSACTION_ALIAS: self._key_transaction_filter_converter,
            "transaction.duration": self._duration_filter_converter,  # Only for dry_run
            "environment": self.builder._environment_filter_converter,
            "transaction": self._transaction_filter_converter,
            "tags[transaction]": self._transaction_filter_converter,
            constants.TITLE_ALIAS: self._transaction_filter_converter,
        }

    @property
    def field_alias_converter(self) -> Mapping[str, Callable[[str], SelectType]]:
        return {
            constants.PROJECT_ALIAS: self._resolve_project_slug_alias,
            constants.PROJECT_NAME_ALIAS: self._resolve_project_slug_alias,
            constants.TEAM_KEY_TRANSACTION_ALIAS: self._resolve_team_key_transaction_alias,
            constants.TITLE_ALIAS: self._resolve_title_alias,
            constants.PROJECT_THRESHOLD_CONFIG_ALIAS: lambda _: self._resolve_project_threshold_config,
            "transaction": self._resolve_transaction_alias,
            "tags[transaction]": self._resolve_transaction_alias,
        }

    def resolve_metric(self, value: str) -> int:
        metric_id = self.resolve_value(constants.METRICS_MAP.get(value, value))
        if metric_id is None:
            # Maybe this is a custom measurment?
            for measurement in self.builder.custom_measurement_map:
                if measurement["name"] == value and measurement["metric_id"] is not None:
                    metric_id = measurement["metric_id"]
        # If its still None its not a custom measurement
        if metric_id is None:
            raise IncompatibleMetricsQuery(f"Metric: {value} could not be resolved")
        self.builder.metric_ids.add(metric_id)
        return metric_id

    def resolve_tag_value(self, value: str) -> Union[str, int]:
        if self.builder.is_performance and self.builder.tag_values_are_strings:
            return value
        return self.resolve_value(value)

    def resolve_value(self, value: str) -> int:
        if self.builder.dry_run:
            return -1
        value_id = self.builder.resolve_metric_index(value)

        return value_id

    def metric_type_resolver(
        self, index: Optional[int] = 0
    ) -> Callable[[List[fields.FunctionArg], Dict[str, Any]], str]:
        """Return the type of the metric, default to duration

        based on fields.reflective_result_type, but in this config since we need the _custom_measurement_cache
        """

        def result_type_fn(
            function_arguments: List[fields.FunctionArg], parameter_values: Dict[str, Any]
        ) -> str:
            argument = function_arguments[index]
            value = parameter_values[argument.name]
            if (
                value == "transaction.duration"
                or is_duration_measurement(value)
                or is_span_op_breakdown(value)
            ):
                return "duration"
            for measurement in self.builder.custom_measurement_map:
                if measurement["name"] == value and measurement["metric_id"] is not None:
                    unit = measurement["unit"]
                    if unit in constants.SIZE_UNITS or unit in constants.DURATION_UNITS:
                        return unit
                    elif unit == "none":
                        return "integer"
                    elif unit in constants.PERCENT_UNITS:
                        return "percentage"
                    else:
                        return "number"
            return "number"

        return result_type_fn

    @property
    def function_converter(self) -> Mapping[str, fields.MetricsFunction]:
        """While the final functions in clickhouse must have their -Merge combinators in order to function, we don't
        need to add them here since snuba has a FunctionMapper that will add it for us. Basically it turns expressions
        like quantiles(0.9)(value) into quantilesMerge(0.9)(percentiles)
        Make sure to update METRIC_FUNCTION_LIST_BY_TYPE when adding functions here, can't be a dynamic list since the
        Metric Layer will actually handle which dataset each function goes to
        """
        resolve_metric_id = {
            "name": "metric_id",
            "fn": lambda args: self.resolve_metric(args["column"]),
        }

        function_converter = {
            function.name: function
            for function in [
                # Note while the discover version of apdex, count_miserable, user_misery
                # accepts arguments, because this is precomputed with tags no parameters
                # are available
                fields.MetricsFunction(
                    "apdex",
                    optional_args=[fields.NullableNumberRange("satisfaction", 0, None)],
                    snql_distribution=self._resolve_apdex_function,
                    default_result_type="number",
                ),
                fields.MetricsFunction(
                    "avg",
                    required_args=[
                        fields.MetricArg(
                            "column",
                            allowed_columns=constants.METRIC_DURATION_COLUMNS,
                        )
                    ],
                    calculated_args=[resolve_metric_id],
                    snql_distribution=lambda args, alias: Function(
                        "avgIf",
                        [
                            Column("value"),
                            Function(
                                "equals",
                                [
                                    Column("metric_id"),
                                    args["metric_id"],
                                ],
                            ),
                        ],
                        alias,
                    ),
                    result_type_fn=self.metric_type_resolver(),
                    default_result_type="integer",
                ),
                fields.MetricsFunction(
                    "count_miserable",
                    required_args=[
                        fields.MetricArg(
                            "column", allowed_columns=["user"], allow_custom_measurements=False
                        )
                    ],
                    optional_args=[fields.NullableNumberRange("satisfaction", 0, None)],
                    calculated_args=[resolve_metric_id],
                    snql_set=self._resolve_count_miserable_function,
                    default_result_type="integer",
                ),
                fields.MetricsFunction(
                    "count_unparameterized_transactions",
                    snql_distribution=lambda args, alias: Function(
                        "countIf",
                        [
                            Function(
                                "and",
                                [
                                    Function(
                                        "equals",
                                        [
                                            Column("metric_id"),
                                            self.resolve_metric("transaction.duration"),
                                        ],
                                    ),
                                    Function(
                                        "equals",
                                        [
                                            self.builder.column("transaction"),
                                            self.builder.resolve_tag_value("<< unparameterized >>"),
                                        ],
                                    ),
                                ],
                            )
                        ],
                        alias,
                    ),
                    # Not yet exposed, need to add far more validation around tag&value
                    private=True,
                    default_result_type="integer",
                ),
                fields.MetricsFunction(
                    "count_null_transactions",
                    snql_distribution=lambda args, alias: Function(
                        "countIf",
                        [
                            Function(
                                "and",
                                [
                                    Function(
                                        "equals",
                                        [
                                            Column("metric_id"),
                                            self.resolve_metric("transaction.duration"),
                                        ],
                                    ),
                                    Function(
                                        "equals",
                                        [
                                            self.builder.column("transaction"),
                                            0,
                                        ],
                                    ),
                                ],
                            )
                        ],
                        alias,
                    ),
                    private=True,
                ),
                fields.MetricsFunction(
                    "count_has_transaction_name",
                    snql_distribution=lambda args, alias: Function(
                        "countIf",
                        [
                            Function(
                                "and",
                                [
                                    Function(
                                        "equals",
                                        [
                                            Column("metric_id"),
                                            self.resolve_metric("transaction.duration"),
                                        ],
                                    ),
                                    Function(
                                        "and",
                                        [
                                            Function(
                                                "notEquals", [self.builder.column("transaction"), 0]
                                            ),
                                            Function(
                                                "notEquals",
                                                [
                                                    self.builder.column("transaction"),
                                                    self.builder.resolve_tag_value(
                                                        "<< unparameterized >>"
                                                    ),
                                                ],
                                            ),
                                        ],
                                    ),
                                ],
                            )
                        ],
                        alias,
                    ),
                    private=True,
                    default_result_type="integer",
                ),
                fields.MetricsFunction(
                    "user_misery",
                    optional_args=[
                        fields.NullableNumberRange("satisfaction", 0, None),
                        fields.with_default(
                            constants.MISERY_ALPHA, fields.NumberRange("alpha", 0, None)
                        ),
                        fields.with_default(
                            constants.MISERY_BETA, fields.NumberRange("beta", 0, None)
                        ),
                    ],
                    calculated_args=[],
                    snql_set=self._resolve_user_misery_function,
                    default_result_type="number",
                ),
                fields.MetricsFunction(
                    "p50",
                    optional_args=[
                        fields.with_default(
                            "transaction.duration",
                            fields.MetricArg(
                                "column", allowed_columns=constants.METRIC_DURATION_COLUMNS
                            ),
                        ),
                    ],
                    calculated_args=[resolve_metric_id],
                    snql_distribution=lambda args, alias: self._resolve_percentile(
                        args, alias, 0.5
                    ),
                    result_type_fn=self.metric_type_resolver(),
                    default_result_type="duration",
                ),
                fields.MetricsFunction(
                    "p75",
                    optional_args=[
                        fields.with_default(
                            "transaction.duration",
                            fields.MetricArg(
                                "column", allowed_columns=constants.METRIC_DURATION_COLUMNS
                            ),
                        ),
                    ],
                    calculated_args=[resolve_metric_id],
                    snql_distribution=lambda args, alias: self._resolve_percentile(
                        args, alias, 0.75
                    ),
                    result_type_fn=self.metric_type_resolver(),
                    default_result_type="duration",
                ),
                fields.MetricsFunction(
                    "p90",
                    optional_args=[
                        fields.with_default(
                            "transaction.duration",
                            fields.MetricArg(
                                "column", allowed_columns=constants.METRIC_DURATION_COLUMNS
                            ),
                        ),
                    ],
                    calculated_args=[resolve_metric_id],
                    snql_distribution=lambda args, alias: self._resolve_percentile(
                        args, alias, 0.90
                    ),
                    result_type_fn=self.metric_type_resolver(),
                    default_result_type="duration",
                ),
                fields.MetricsFunction(
                    "p95",
                    optional_args=[
                        fields.with_default(
                            "transaction.duration",
                            fields.MetricArg(
                                "column", allowed_columns=constants.METRIC_DURATION_COLUMNS
                            ),
                        ),
                    ],
                    calculated_args=[resolve_metric_id],
                    snql_distribution=lambda args, alias: self._resolve_percentile(
                        args, alias, 0.95
                    ),
                    result_type_fn=self.metric_type_resolver(),
                    default_result_type="duration",
                ),
                fields.MetricsFunction(
                    "p99",
                    optional_args=[
                        fields.with_default(
                            "transaction.duration",
                            fields.MetricArg(
                                "column", allowed_columns=constants.METRIC_DURATION_COLUMNS
                            ),
                        ),
                    ],
                    calculated_args=[resolve_metric_id],
                    snql_distribution=lambda args, alias: self._resolve_percentile(
                        args, alias, 0.99
                    ),
                    result_type_fn=self.metric_type_resolver(),
                    default_result_type="duration",
                ),
                fields.MetricsFunction(
                    "p100",
                    optional_args=[
                        fields.with_default(
                            "transaction.duration",
                            fields.MetricArg(
                                "column", allowed_columns=constants.METRIC_DURATION_COLUMNS
                            ),
                        ),
                    ],
                    calculated_args=[resolve_metric_id],
                    snql_distribution=lambda args, alias: self._resolve_percentile(args, alias, 1),
                    result_type_fn=self.metric_type_resolver(),
                    default_result_type="duration",
                ),
                fields.MetricsFunction(
                    "max",
                    required_args=[
                        fields.MetricArg("column"),
                    ],
                    calculated_args=[resolve_metric_id],
                    snql_distribution=lambda args, alias: Function(
                        "maxIf",
                        [
                            Column("value"),
                            Function("equals", [Column("metric_id"), args["metric_id"]]),
                        ],
                        alias,
                    ),
                    result_type_fn=self.metric_type_resolver(),
                ),
                fields.MetricsFunction(
                    "min",
                    required_args=[
                        fields.MetricArg("column"),
                    ],
                    calculated_args=[resolve_metric_id],
                    snql_distribution=lambda args, alias: Function(
                        "minIf",
                        [
                            Column("value"),
                            Function("equals", [Column("metric_id"), args["metric_id"]]),
                        ],
                        alias,
                    ),
                    result_type_fn=self.metric_type_resolver(),
                ),
                fields.MetricsFunction(
                    "sum",
                    required_args=[
                        fields.MetricArg("column"),
                    ],
                    calculated_args=[resolve_metric_id],
                    snql_distribution=lambda args, alias: Function(
                        "sumIf",
                        [
                            Column("value"),
                            Function("equals", [Column("metric_id"), args["metric_id"]]),
                        ],
                        alias,
                    ),
                    result_type_fn=self.metric_type_resolver(),
                ),
                fields.MetricsFunction(
                    "sumIf",
                    required_args=[
                        fields.ColumnTagArg("if_col"),
                        fields.FunctionArg("if_val"),
                    ],
                    calculated_args=[
                        {
                            "name": "resolved_val",
                            "fn": lambda args: self.builder.resolve_tag_value(args["if_val"]),
                        }
                    ],
                    snql_counter=lambda args, alias: Function(
                        "sumIf",
                        [
                            Column("value"),
                            Function("equals", [args["if_col"], args["resolved_val"]]),
                        ],
                        alias,
                    ),
                    default_result_type="integer",
                ),
                fields.MetricsFunction(
                    "percentile",
                    required_args=[
                        fields.with_default(
                            "transaction.duration",
                            fields.MetricArg(
                                "column", allowed_columns=constants.METRIC_DURATION_COLUMNS
                            ),
                        ),
                        fields.NumberRange("percentile", 0, 1),
                    ],
                    calculated_args=[resolve_metric_id],
                    snql_distribution=self._resolve_percentile,
                    result_type_fn=self.metric_type_resolver(),
                    default_result_type="duration",
                ),
                fields.MetricsFunction(
                    "count_unique",
                    required_args=[
                        fields.MetricArg(
                            "column", allowed_columns=["user"], allow_custom_measurements=False
                        )
                    ],
                    calculated_args=[resolve_metric_id],
                    snql_set=lambda args, alias: Function(
                        "uniqIf",
                        [
                            Column("value"),
                            Function("equals", [Column("metric_id"), args["metric_id"]]),
                        ],
                        alias,
                    ),
                    default_result_type="integer",
                ),
                fields.MetricsFunction(
                    "uniq",
                    snql_set=lambda args, alias: Function(
                        "uniq",
                        [Column("value")],
                        alias,
                    ),
                ),
                fields.MetricsFunction(
                    "uniqIf",
                    required_args=[
                        fields.ColumnTagArg("if_col"),
                        fields.FunctionArg("if_val"),
                    ],
                    calculated_args=[
                        {
                            "name": "resolved_val",
                            "fn": lambda args: self.builder.resolve_tag_value(args["if_val"]),
                        }
                    ],
                    snql_set=lambda args, alias: Function(
                        "uniqIf",
                        [
                            Column("value"),
                            Function("equals", [args["if_col"], args["resolved_val"]]),
                        ],
                        alias,
                    ),
                    default_result_type="integer",
                ),
                fields.MetricsFunction(
                    "count",
                    snql_distribution=lambda args, alias: Function(
                        "countIf",
                        [
                            Column("value"),
                            Function(
                                "equals",
                                [
                                    Column("metric_id"),
                                    self.resolve_metric("transaction.duration"),
                                ],
                            ),
                        ],
                        alias,
                    ),
                    default_result_type="integer",
                ),
                fields.MetricsFunction(
                    "count_web_vitals",
                    required_args=[
                        fields.MetricArg(
                            "column",
                            allowed_columns=[
                                "measurements.fp",
                                "measurements.fcp",
                                "measurements.lcp",
                                "measurements.fid",
                                "measurements.cls",
                            ],
                            allow_custom_measurements=False,
                        ),
                        fields.SnQLStringArg(
                            "quality", allowed_strings=["good", "meh", "poor", "any"]
                        ),
                    ],
                    calculated_args=[resolve_metric_id],
                    snql_distribution=self._resolve_web_vital_function,
                    default_result_type="integer",
                ),
                fields.MetricsFunction(
                    "epm",
                    snql_distribution=lambda args, alias: Function(
                        "divide",
                        [
                            Function(
                                "countIf",
                                [
                                    Column("value"),
                                    Function(
                                        "equals",
                                        [
                                            Column("metric_id"),
                                            self.resolve_metric("transaction.duration"),
                                        ],
                                    ),
                                ],
                            ),
                            Function("divide", [args["interval"], 60]),
                        ],
                        alias,
                    ),
                    optional_args=[fields.IntervalDefault("interval", 1, None)],
                    default_result_type="number",
                ),
                fields.MetricsFunction(
                    "eps",
                    snql_distribution=lambda args, alias: Function(
                        "divide",
                        [
                            Function(
                                "countIf",
                                [
                                    Column("value"),
                                    Function(
                                        "equals",
                                        [
                                            Column("metric_id"),
                                            self.resolve_metric("transaction.duration"),
                                        ],
                                    ),
                                ],
                            ),
                            args["interval"],
                        ],
                        alias,
                    ),
                    optional_args=[fields.IntervalDefault("interval", 1, None)],
                    default_result_type="number",
                ),
                fields.MetricsFunction(
                    "failure_count",
                    snql_distribution=self._resolve_failure_count,
                    default_result_type="integer",
                ),
                fields.MetricsFunction(
                    "failure_rate",
                    snql_distribution=lambda args, alias: Function(
                        "divide",
                        [
                            self._resolve_failure_count(args),
                            Function(
                                "countIf",
                                [
                                    Column("value"),
                                    Function(
                                        "equals",
                                        [
                                            Column("metric_id"),
                                            self.resolve_metric("transaction.duration"),
                                        ],
                                    ),
                                ],
                            ),
                        ],
                        alias,
                    ),
                    default_result_type="percentage",
                ),
                fields.MetricsFunction(
                    "histogram",
                    required_args=[fields.MetricArg("column")],
                    calculated_args=[resolve_metric_id],
                    snql_distribution=self._resolve_histogram_function,
                    default_result_type="number",
                    private=True,
                ),
            ]
        }

        for alias, name in constants.FUNCTION_ALIASES.items():
            if name in function_converter:
                function_converter[alias] = function_converter[name].alias_as(alias)

        return function_converter

    # Field Aliases
    def _resolve_title_alias(self, alias: str) -> SelectType:
        """title == transaction in discover"""
        return self._resolve_transaction_alias(alias)

    def _resolve_team_key_transaction_alias(self, _: str) -> SelectType:
        if self.builder.dry_run:
            return field_aliases.dry_run_default(self.builder, constants.TEAM_KEY_TRANSACTION_ALIAS)
        return field_aliases.resolve_team_key_transaction_alias(
            self.builder, resolve_metric_index=True
        )

    def _resolve_project_slug_alias(self, alias: str) -> SelectType:
        if self.builder.dry_run:
            return field_aliases.dry_run_default(self.builder, alias)
        return field_aliases.resolve_project_slug_alias(self.builder, alias)

    def _resolve_transaction_alias(self, alias: str) -> SelectType:
        return Function(
            "transform",
            [
                Column(f"tags[{self.resolve_value('transaction')}]"),
                [0 if not self.builder.tag_values_are_strings else ""],
                [self.builder.resolve_tag_value("<< unparameterized >>")],
            ],
            alias,
        )

    @cached_property
    def _resolve_project_threshold_config(self) -> SelectType:
        org_id = self.builder.params.get("organization_id")
        project_ids = self.builder.params.get("project_id")

        project_threshold_configs = (
            ProjectTransactionThreshold.objects.filter(
                organization_id=org_id,
                project_id__in=project_ids,
            )
            .order_by("project_id")
            .values_list("project_id", "metric")
        )

        transaction_threshold_configs = (
            ProjectTransactionThresholdOverride.objects.filter(
                organization_id=org_id,
                project_id__in=project_ids,
            )
            .order_by("project_id")
            .values_list("transaction", "project_id", "metric")
        )

        num_project_thresholds = project_threshold_configs.count()
        sentry_sdk.set_tag("project_threshold.count", num_project_thresholds)
        sentry_sdk.set_tag(
            "project_threshold.count.grouped",
            format_grouped_length(num_project_thresholds, [10, 100, 250, 500]),
        )

        num_transaction_thresholds = transaction_threshold_configs.count()
        sentry_sdk.set_tag("txn_threshold.count", num_transaction_thresholds)
        sentry_sdk.set_tag(
            "txn_threshold.count.grouped",
            format_grouped_length(num_transaction_thresholds, [10, 100, 250, 500]),
        )

        if (
            num_project_thresholds + num_transaction_thresholds
            > constants.MAX_QUERYABLE_TRANSACTION_THRESHOLDS
        ):
            raise InvalidSearchQuery(
                f"Exceeded {constants.MAX_QUERYABLE_TRANSACTION_THRESHOLDS} configured transaction thresholds limit, try with fewer Projects."
            )

        # Arrays need to have toUint64 casting because clickhouse will define the type as the narrowest possible type
        # that can store listed argument types, which means the comparison will fail because of mismatched types
        project_thresholds = {}
        project_threshold_config_keys = []
        project_threshold_config_values = []
        for project_id, metric in project_threshold_configs:
            metric = TRANSACTION_METRICS[metric]
            if metric == constants.DEFAULT_PROJECT_THRESHOLD_METRIC:
                # small optimization, if the configuration is equal to the default,
                # we can skip it in the final query
                continue

            project_thresholds[project_id] = metric
            project_threshold_config_keys.append(Function("toUInt64", [project_id]))
            project_threshold_config_values.append(metric)

        project_threshold_override_config_keys = []
        project_threshold_override_config_values = []
        for transaction, project_id, metric in transaction_threshold_configs:
            metric = TRANSACTION_METRICS[metric]
            if project_id in project_thresholds and metric == project_thresholds[project_id][0]:
                # small optimization, if the configuration is equal to the project
                # configs, we can skip it in the final query
                continue

            elif (
                project_id not in project_thresholds
                and metric == constants.DEFAULT_PROJECT_THRESHOLD_METRIC
            ):
                # small optimization, if the configuration is equal to the default
                # and no project configs were set, we can skip it in the final query
                continue

            transaction_id = self.builder.resolve_tag_value(transaction)
            # Don't add to the config if we can't resolve it
            if transaction_id is None:
                continue
            project_threshold_override_config_keys.append(
                (Function("toUInt64", [project_id]), (Function("toUInt64", [transaction_id])))
            )
            project_threshold_override_config_values.append(metric)

        project_threshold_config_index: SelectType = Function(
            "indexOf",
            [
                project_threshold_config_keys,
                self.builder.column("project_id"),
            ],
            constants.PROJECT_THRESHOLD_CONFIG_INDEX_ALIAS,
        )

        project_threshold_override_config_index: SelectType = Function(
            "indexOf",
            [
                project_threshold_override_config_keys,
                (self.builder.column("project_id"), self.builder.column("transaction")),
            ],
            constants.PROJECT_THRESHOLD_OVERRIDE_CONFIG_INDEX_ALIAS,
        )

        def _project_threshold_config(alias: Optional[str] = None) -> SelectType:
            if project_threshold_config_keys and project_threshold_config_values:
                return Function(
                    "if",
                    [
                        Function(
                            "equals",
                            [
                                project_threshold_config_index,
                                0,
                            ],
                        ),
                        constants.DEFAULT_PROJECT_THRESHOLD_METRIC,
                        Function(
                            "arrayElement",
                            [
                                project_threshold_config_values,
                                project_threshold_config_index,
                            ],
                        ),
                    ],
                    alias,
                )

            return Function(
                "toString",
                [constants.DEFAULT_PROJECT_THRESHOLD_METRIC],
            )

        if project_threshold_override_config_keys and project_threshold_override_config_values:
            return Function(
                "if",
                [
                    Function(
                        "equals",
                        [
                            project_threshold_override_config_index,
                            0,
                        ],
                    ),
                    _project_threshold_config(),
                    Function(
                        "arrayElement",
                        [
                            project_threshold_override_config_values,
                            project_threshold_override_config_index,
                        ],
                    ),
                ],
                constants.PROJECT_THRESHOLD_CONFIG_ALIAS,
            )

        return _project_threshold_config(constants.PROJECT_THRESHOLD_CONFIG_ALIAS)

    def _project_threshold_multi_if_function(self) -> SelectType:
        """Accessed by `_resolve_apdex_function` and `_resolve_count_miserable_function`,
        this returns the right duration value (for example, lcp or duration) based
        on project or transaction thresholds that have been configured by the user.
        """

        return Function(
            "multiIf",
            [
                Function(
                    "equals",
                    [
                        self.builder.resolve_field_alias("project_threshold_config"),
                        "lcp",
                    ],
                ),
                self.resolve_metric("measurements.lcp"),
                self.resolve_metric("transaction.duration"),
            ],
        )

    # Query Filters
    def _event_type_converter(self, search_filter: SearchFilter) -> Optional[WhereType]:
        """Not really a converter, check its transaction, error otherwise"""
        value = search_filter.value.value
        if value == "transaction":
            return None

        raise IncompatibleMetricsQuery("Can only filter event.type:transaction")

    def _project_slug_filter_converter(self, search_filter: SearchFilter) -> Optional[WhereType]:
        return filter_aliases.project_slug_converter(self.builder, search_filter)

    def _release_filter_converter(self, search_filter: SearchFilter) -> Optional[WhereType]:
        return filter_aliases.release_filter_converter(self.builder, search_filter)

    def _duration_filter_converter(self, search_filter: SearchFilter) -> Optional[WhereType]:
        if (
            self.builder.dry_run
            and search_filter.value.raw_value == 900000
            and search_filter.operator == "<"
        ):
            return None

        return self.builder._default_filter_converter(search_filter)

    def _transaction_filter_converter(self, search_filter: SearchFilter) -> Optional[WhereType]:
        operator = search_filter.operator
        value = search_filter.value.value

        if operator in ("=", "!=") and value == "":
            # !has:transaction
            if operator == "=":
                raise InvalidSearchQuery(
                    "All events have a transaction so this query wouldn't return anything"
                )
            else:
                # All events have a "transaction" since we map null -> unparam so no need to filter
                return None

        if isinstance(value, list):
            value = [self.builder.resolve_tag_value(v) for v in value]
        else:
            value = self.builder.resolve_tag_value(value)

        return Condition(Column(f"tags[{self.resolve_value('transaction')}]"), Op(operator), value)

    # Query Functions
    def _resolve_count_if(
        self,
        metric_condition: Function,
        condition: Function,
        alias: Optional[str] = None,
    ) -> SelectType:
        return Function(
            "countIf",
            [
                Column("value"),
                Function(
                    "and",
                    [
                        metric_condition,
                        condition,
                    ],
                ),
            ],
            alias,
        )

    def _resolve_apdex_function(
        self,
        args: Mapping[str, Union[str, Column, SelectType, int, float]],
        alias: Optional[str] = None,
    ) -> SelectType:
        """Apdex is tag based in metrics, which means we can't base it on the satsifaction parameter"""
        if args["satisfaction"] is not None:
            raise IncompatibleMetricsQuery(
                "Cannot query apdex with a threshold parameter on the metrics dataset"
            )

        metric_satisfied = self.builder.resolve_tag_value(constants.METRIC_SATISFIED_TAG_VALUE)
        metric_tolerated = self.builder.resolve_tag_value(constants.METRIC_TOLERATED_TAG_VALUE)

        # Nothing is satisfied or tolerated, the score must be 0
        if metric_satisfied is None and metric_tolerated is None:
            return Function(
                "toUInt64",
                [0],
                alias,
            )

        satisfied = Function(
            "equals", [self.builder.column(constants.METRIC_SATISFACTION_TAG_KEY), metric_satisfied]
        )
        tolerable = Function(
            "equals", [self.builder.column(constants.METRIC_SATISFACTION_TAG_KEY), metric_tolerated]
        )
        metric_condition = Function(
            "equals", [Column("metric_id"), self._project_threshold_multi_if_function()]
        )

        return Function(
            "divide",
            [
                Function(
                    "plus",
                    [
                        self._resolve_count_if(metric_condition, satisfied),
                        Function(
                            "divide",
                            [self._resolve_count_if(metric_condition, tolerable), 2],
                        ),
                    ],
                ),
                Function("countIf", [Column("value"), metric_condition]),
            ],
            alias,
        )

    def _resolve_histogram_function(
        self,
        args: Mapping[str, Union[str, Column, SelectType, int, float]],
        alias: Optional[str] = None,
    ) -> SelectType:
        """zoom_params is based on running metrics zoom_histogram function that adds conditions based on min, max,
        buckets"""
        zoom_params = getattr(self.builder, "zoom_params", None)
        num_buckets = getattr(self.builder, "num_buckets", 250)
        metric_condition = Function("equals", [Column("metric_id"), args["metric_id"]])
        self.builder.histogram_aliases.append(alias)
        return Function(
            f"histogramIf({num_buckets})",
            [
                Column("value"),
                Function("and", [zoom_params, metric_condition])
                if zoom_params
                else metric_condition,
            ],
            alias,
        )

    def _resolve_count_miserable_function(
        self,
        args: Mapping[str, Union[str, Column, SelectType, int, float]],
        alias: Optional[str] = None,
    ) -> SelectType:
        if args["satisfaction"] is not None:
            raise IncompatibleMetricsQuery(
                "Cannot query misery with a threshold parameter on the metrics dataset"
            )
        metric_frustrated = self.builder.resolve_tag_value(constants.METRIC_FRUSTRATED_TAG_VALUE)

        # Nobody is miserable, we can return 0
        if metric_frustrated is None:
            return Function(
                "toUInt64",
                [0],
                alias,
            )

        return Function(
            "uniqIf",
            [
                Column("value"),
                Function(
                    "and",
                    [
                        Function(
                            "equals",
                            [
                                Column("metric_id"),
                                args["metric_id"],
                            ],
                        ),
                        Function(
                            "equals",
                            [
                                self.builder.column(constants.METRIC_SATISFACTION_TAG_KEY),
                                metric_frustrated,
                            ],
                        ),
                    ],
                ),
            ],
            alias,
        )

    def _resolve_user_misery_function(
        self,
        args: Mapping[str, Union[str, Column, SelectType, int, float]],
        alias: Optional[str] = None,
    ) -> SelectType:
        if args["satisfaction"] is not None:
            raise IncompatibleMetricsQuery(
                "Cannot query user_misery with a threshold parameter on the metrics dataset"
            )
        return Function(
            "divide",
            [
                Function(
                    "plus",
                    [
                        self.builder.resolve_function("count_miserable(user)"),
                        args["alpha"],
                    ],
                ),
                Function(
                    "plus",
                    [
                        Function(
                            "nullIf", [self.builder.resolve_function("count_unique(user)"), 0]
                        ),
                        args["alpha"] + args["beta"],
                    ],
                ),
            ],
            alias,
        )

    def _resolve_failure_count(
        self,
        _: Mapping[str, Union[str, Column, SelectType, int, float]],
        alias: Optional[str] = None,
    ) -> SelectType:
        statuses = [
            self.builder.resolve_tag_value(status) for status in constants.NON_FAILURE_STATUS
        ]
        return self._resolve_count_if(
            Function(
                "equals",
                [
                    Column("metric_id"),
                    self.resolve_metric("transaction.duration"),
                ],
            ),
            Function(
                "notIn",
                [
                    self.builder.column("transaction.status"),
                    list(status for status in statuses if status is not None),
                ],
            ),
            alias,
        )

    def _resolve_percentile(
        self,
        args: Mapping[str, Union[str, Column, SelectType, int, float]],
        alias: str,
        fixed_percentile: Optional[float] = None,
    ) -> SelectType:
        if fixed_percentile is None:
            fixed_percentile = args["percentile"]
        if fixed_percentile not in constants.METRIC_PERCENTILES:
            raise IncompatibleMetricsQuery("Custom quantile incompatible with metrics")
        return (
            Function(
                "maxIf",
                [
                    Column("value"),
                    Function("equals", [Column("metric_id"), args["metric_id"]]),
                ],
                alias,
            )
            if fixed_percentile == 1
            else Function(
                "arrayElement",
                [
                    Function(
                        f"quantilesIf({fixed_percentile})",
                        [
                            Column("value"),
                            Function("equals", [Column("metric_id"), args["metric_id"]]),
                        ],
                    ),
                    1,
                ],
                alias,
            )
        )

    def _key_transaction_filter_converter(self, search_filter: SearchFilter) -> Optional[WhereType]:
        return filter_aliases.team_key_transaction_filter(self.builder, search_filter)

    def _resolve_web_vital_function(
        self,
        args: Mapping[str, Union[str, Column, SelectType, int, float]],
        alias: str,
    ) -> SelectType:
        column = args["column"]
        metric_id = args["metric_id"]
        quality = args["quality"].lower()

        if column not in [
            "measurements.lcp",
            "measurements.fcp",
            "measurements.fp",
            "measurements.fid",
            "measurements.cls",
        ]:
            raise InvalidSearchQuery("count_web_vitals only supports measurements")

        measurement_rating = self.builder.resolve_column("measurement_rating")

        if quality == "any":
            return Function(
                "countIf",
                [
                    Column("value"),
                    Function("equals", [Column("metric_id"), metric_id]),
                ],
                alias,
            )

        try:
            quality_id = self.builder.resolve_tag_value(quality)
        except IncompatibleMetricsQuery:
            quality_id = None

        if quality_id is None:
            return Function(
                # This matches the type from doing `select toTypeName(count()) ...` from clickhouse
                "toUInt64",
                [0],
                alias,
            )

        return Function(
            "countIf",
            [
                Column("value"),
                Function(
                    "and",
                    [
                        Function("equals", [measurement_rating, quality_id]),
                        Function("equals", [Column("metric_id"), metric_id]),
                    ],
                ),
            ],
            alias,
        )
