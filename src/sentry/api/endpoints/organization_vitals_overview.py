import logging
from datetime import timedelta
from typing import Sequence

from django.conf import settings
from django.core.cache import cache
from django.http import Http404
from django.utils import timezone
from rest_framework.request import Request
from rest_framework.response import Response

from sentry import experiments
from sentry.api.base import ONE_HOUR
from sentry.api.bases import OrganizationEventsEndpointBase
from sentry.api.serializers.models.project import get_access_by_project
from sentry.models import Organization, Project, ProjectStatus
from sentry.snuba import discover
from sentry.utils import json

logger = logging.getLogger(__name__)

CACHE_TIME = ONE_HOUR * 4

# Web vitals: p75 for LCP and FCP
# Mobile vitals: Cold Start and Warm Start
# Snuba names to the API layer that matches the TS definition
NAME_MAPPING = {
    "p75_measurements_fcp": "FCP",
    "p75_measurements_lcp": "LCP",
    "p75_measurements_app_start_warm": "appStartWarm",
    "p75_measurements_app_start_cold": "appStartCold",
    "count_if_measurements_fcp_greaterOrEquals_0": "fcpCount",
    "count_if_measurements_lcp_greaterOrEquals_0": "lcpCount",
    "count_if_measurements_app_start_warm_greaterOrEquals_0": "appWarmStartCount",
    "count_if_measurements_app_start_cold_greaterOrEquals_0": "appColdStartCount",
    "project_id": "projectId",
}

# common columns we use for every query
BASIC_COLUMNS = [
    "p75(measurements.lcp)",
    "p75(measurements.fcp)",
    "p75(measurements.app_start_cold)",
    "p75(measurements.app_start_warm)",
    "count_if(measurements.lcp,greaterOrEquals,0)",
    "count_if(measurements.fcp,greaterOrEquals,0)",
    "count_if(measurements.app_start_cold,greaterOrEquals,0)",
    "count_if(measurements.app_start_warm,greaterOrEquals,0)",
]

# if we don't have valid results or we have too many projects we can return this instead
NO_RESULT_RESPONSE = {
    "FCP": None,
    "LCP": None,
    "appStartWarm": None,
    "appStartCold": None,
    "fcpCount": 0,
    "lcpCount": 0,
    "appColdStartCount": 0,
    "appWarmStartCount": 0,
    "projectData": [],
}


def get_vital_data_for_org_no_cache(organization: Organization, projects: Sequence[Project]):
    project_ids = list(map(lambda x: x.id, projects))

    def get_discover_result(columns, referrer):
        result = discover.query(
            query="transaction.duration:<15m event.type:transaction",
            selected_columns=columns,
            limit=settings.ORGANIZATION_VITALS_OVERVIEW_PROJECT_LIMIT,
            params={
                "start": timezone.now() - timedelta(days=7),
                "end": timezone.now(),
                "organization_id": organization.id,
                "project_id": list(project_ids),
            },
            referrer=referrer,
        )
        logger.info(
            "get_discover_result",
            extra={
                "organization_id": organization.id,
                "num_projects": len(projects),
                "columns": json.dumps(columns),
                "data": json.dumps(result["data"]),
            },
        )
        return result["data"]

    org_data = get_discover_result(BASIC_COLUMNS, "api.organization-vitals")
    # no data at all for any vital
    if not org_data:
        return (None, None)

    # get counts by project
    project_data = get_discover_result(
        ["project_id"] + BASIC_COLUMNS, "api.organization-vitals-per-project"
    )
    return (org_data, project_data)


def get_vital_data_for_org(organization: Organization, projects: Sequence[Project]):
    # cache is unique to an org
    cache_key = f"organization-vitals-overview:{organization.id}"
    cache_value = cache.get(cache_key)
    # cache miss, lookup and store value
    if cache_value is None:
        cache_value = get_vital_data_for_org_no_cache(organization, projects)
        cache.set(cache_key, cache_value, CACHE_TIME)
    return cache_value


class OrganizationVitalsOverviewEndpoint(OrganizationEventsEndpointBase):
    private = True

    def get(self, request: Request, organization: Organization) -> Response:
        # only can access endpint with experiment
        if not experiments.get("VitalsAlertExperiment", organization, request.user):
            raise Http404

        # TODO: add caching
        # try to get all the projects for the org even though it's possible they don't have access
        projects = Project.objects.filter(organization=organization, status=ProjectStatus.VISIBLE)[
            0 : settings.ORGANIZATION_VITALS_OVERVIEW_PROJECT_LIMIT
        ]

        # if we are at the limit, then it's likely we didn't get every project in the org
        # so the result we are returning for the organization aggregatation would not be accurate
        # as result, just return the payload for no data so the UI won't display the banner
        if len(projects) >= settings.ORGANIZATION_VITALS_OVERVIEW_PROJECT_LIMIT:
            logger.info(
                "too_many_projects",
                extra={"organization_id": organization.id, "num_projects": len(projects)},
            )
            return self.respond(NO_RESULT_RESPONSE)

        with self.handle_query_errors():
            # find data we might have cached
            org_data, project_data = get_vital_data_for_org(organization, projects)
            # no data at all for any vital
            if not org_data:
                logger.info(
                    "no_org_data",
                    extra={"organization_id": organization.id},
                )
                return self.respond(NO_RESULT_RESPONSE)

            # take data and transform output
            output = {}
            # only a single result
            for key, val in org_data[0].items():
                output[NAME_MAPPING[key]] = val

            # check access for project level data
            access_by_project = get_access_by_project(projects, request.user)
            projects_with_access = list(
                filter(lambda proj: access_by_project[proj]["has_access"], projects)
            )
            project_ids_with_access = set(map(lambda proj: proj.id, projects_with_access))

            output["projectData"] = []
            for one_project_data in project_data:
                # skip ones with no access
                if one_project_data["project_id"] not in project_ids_with_access:
                    continue
                mapped_project_data = {}
                # for each project, map the data
                for key, val in one_project_data.items():
                    mapped_project_data[NAME_MAPPING[key]] = val
                output["projectData"].append(mapped_project_data)

            return self.respond(output)
