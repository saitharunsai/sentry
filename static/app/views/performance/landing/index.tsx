import {FC, Fragment, useEffect, useRef} from 'react';
import {browserHistory, InjectedRouter} from 'react-router';
import styled from '@emotion/styled';
import {Location} from 'history';

import Button from 'sentry/components/button';
import ButtonBar from 'sentry/components/buttonBar';
import DatePageFilter from 'sentry/components/datePageFilter';
import EnvironmentPageFilter from 'sentry/components/environmentPageFilter';
import SearchBar from 'sentry/components/events/searchBar';
import * as Layout from 'sentry/components/layouts/thirds';
import LoadingIndicator from 'sentry/components/loadingIndicator';
import PageFilterBar from 'sentry/components/organizations/pageFilterBar';
import PageHeading from 'sentry/components/pageHeading';
import TransactionNameSearchBar from 'sentry/components/performance/searchBar';
import * as TeamKeyTransactionManager from 'sentry/components/performance/teamKeyTransactionsManager';
import ProjectPageFilter from 'sentry/components/projectPageFilter';
import {MAX_QUERY_LENGTH} from 'sentry/constants';
import {t} from 'sentry/locale';
import {PageContent} from 'sentry/styles/organization';
import space from 'sentry/styles/space';
import {Organization, PageFilters, Project} from 'sentry/types';
import EventView from 'sentry/utils/discover/eventView';
import {generateAggregateFields} from 'sentry/utils/discover/fields';
import {GenericQueryBatcher} from 'sentry/utils/performance/contexts/genericQueryBatcher';
import {
  canUseMetricsData,
  MEPConsumer,
  MEPSettingProvider,
  MEPState,
} from 'sentry/utils/performance/contexts/metricsEnhancedSetting';
import {
  PageErrorAlert,
  PageErrorProvider,
} from 'sentry/utils/performance/contexts/pageError';
import {MutableSearch} from 'sentry/utils/tokenizeSearch';
import useTeams from 'sentry/utils/useTeams';

import Onboarding from '../onboarding';
import {MetricsEventsDropdown} from '../transactionSummary/transactionOverview/metricEvents/metricsEventsDropdown';
import {getTransactionSearchQuery} from '../utils';

import {AllTransactionsView} from './views/allTransactionsView';
import {BackendView} from './views/backendView';
import {FrontendOtherView} from './views/frontendOtherView';
import {FrontendPageloadView} from './views/frontendPageloadView';
import {MobileView} from './views/mobileView';
import {MetricsDataSwitcher} from './metricsDataSwitcher';
import {MetricsDataSwitcherAlert} from './metricsDataSwitcherAlert';
import {
  getDefaultDisplayForPlatform,
  getLandingDisplayFromParam,
  handleLandingDisplayChange,
  LANDING_DISPLAYS,
  LandingDisplayField,
} from './utils';

type Props = {
  eventView: EventView;
  handleSearch: (searchQuery: string, currentMEPState?: MEPState) => void;
  handleTrendsClick: () => void;
  location: Location;
  onboardingProject: Project | undefined;
  organization: Organization;
  projects: Project[];
  router: InjectedRouter;
  selection: PageFilters;
  setError: (msg: string | undefined) => void;
  withStaticFilters: boolean;
};

const fieldToViewMap: Record<LandingDisplayField, FC<Props>> = {
  [LandingDisplayField.ALL]: AllTransactionsView,
  [LandingDisplayField.BACKEND]: BackendView,
  [LandingDisplayField.FRONTEND_OTHER]: FrontendOtherView,
  [LandingDisplayField.FRONTEND_PAGELOAD]: FrontendPageloadView,
  [LandingDisplayField.MOBILE]: MobileView,
};

export function PerformanceLanding(props: Props) {
  const {
    organization,
    location,
    eventView,
    projects,
    handleSearch,
    handleTrendsClick,
    onboardingProject,
  } = props;

  const {teams, initiallyLoaded} = useTeams({provideUserTeams: true});

  const hasMounted = useRef(false);
  const paramLandingDisplay = getLandingDisplayFromParam(location);
  const defaultLandingDisplayForProjects = getDefaultDisplayForPlatform(
    projects,
    eventView
  );
  const landingDisplay = paramLandingDisplay ?? defaultLandingDisplayForProjects;
  const showOnboarding = onboardingProject !== undefined;

  useEffect(() => {
    if (hasMounted.current) {
      browserHistory.replace({
        pathname: location.pathname,
        query: {
          ...location.query,
          landingDisplay: undefined,
        },
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [eventView.project.join('.')]);

  useEffect(() => {
    hasMounted.current = true;
  }, []);

  const getFreeTextFromQuery = (query: string) => {
    const conditions = new MutableSearch(query);
    const transactionValues = conditions.getFilterValues('transaction');
    if (transactionValues.length) {
      return transactionValues[0];
    }
    return '';
  };

  const derivedQuery = getTransactionSearchQuery(location, eventView.query);

  const ViewComponent = fieldToViewMap[landingDisplay.field];

  let pageFilters: React.ReactNode = (
    <PageFilterBar condensed>
      <ProjectPageFilter />
      <EnvironmentPageFilter />
      <DatePageFilter alignDropdown="left" />
    </PageFilterBar>
  );

  if (showOnboarding) {
    pageFilters = <SearchContainerWithFilter>{pageFilters}</SearchContainerWithFilter>;
  }

  const SearchFilterContainer = organization.features.includes('performance-use-metrics')
    ? SearchContainerWithFilterAndMetrics
    : SearchContainerWithFilter;

  const shouldShowTransactionNameOnlySearch = canUseMetricsData(organization);

  return (
    <StyledPageContent data-test-id="performance-landing-v3">
      <PageErrorProvider>
        <Layout.Header>
          <Layout.HeaderContent>
            <StyledHeading>{t('Performance')}</StyledHeading>
          </Layout.HeaderContent>
          <Layout.HeaderActions>
            {!showOnboarding && (
              <ButtonBar gap={3}>
                <Button
                  priority="primary"
                  data-test-id="landing-header-trends"
                  onClick={() => handleTrendsClick()}
                >
                  {t('View Trends')}
                </Button>
              </ButtonBar>
            )}
          </Layout.HeaderActions>

          <Layout.HeaderNavTabs>
            {LANDING_DISPLAYS.map(({label, field}) => (
              <li key={label} className={landingDisplay.field === field ? 'active' : ''}>
                <a
                  href="#"
                  data-test-id={`landing-tab-${field}`}
                  onClick={() =>
                    handleLandingDisplayChange(
                      field,
                      location,
                      projects,
                      organization,
                      eventView
                    )
                  }
                >
                  {t(label)}
                </a>
              </li>
            ))}
          </Layout.HeaderNavTabs>
        </Layout.Header>
        <Layout.Body data-test-id="performance-landing-body">
          <Layout.Main fullWidth>
            <MetricsDataSwitcher
              organization={organization}
              eventView={eventView}
              location={location}
            >
              {metricsDataSide => (
                <MEPSettingProvider
                  location={location}
                  forceTransactions={metricsDataSide.forceTransactionsOnly}
                >
                  <MetricsDataSwitcherAlert
                    organization={organization}
                    eventView={eventView}
                    projects={projects}
                    location={location}
                    router={props.router}
                    {...metricsDataSide}
                  />
                  <PageErrorAlert />
                  {showOnboarding ? (
                    <Fragment>
                      {pageFilters}
                      <Onboarding
                        organization={organization}
                        project={onboardingProject}
                      />
                    </Fragment>
                  ) : (
                    <Fragment>
                      <SearchFilterContainer>
                        {pageFilters}
                        <MEPConsumer>
                          {({metricSettingState}) => {
                            const searchQuery =
                              metricSettingState === MEPState.metricsOnly
                                ? getFreeTextFromQuery(derivedQuery)
                                : derivedQuery;

                            return metricSettingState === MEPState.metricsOnly &&
                              shouldShowTransactionNameOnlySearch ? (
                              // TODO replace `handleSearch prop` with transaction name search once
                              // transaction name search becomes the default search bar
                              <TransactionNameSearchBar
                                organization={organization}
                                location={location}
                                eventView={eventView}
                                onSearch={(query: string) =>
                                  handleSearch(query, metricSettingState)
                                }
                                query={searchQuery}
                              />
                            ) : (
                              <SearchBar
                                searchSource="performance_landing"
                                organization={organization}
                                projectIds={eventView.project}
                                query={searchQuery}
                                fields={generateAggregateFields(
                                  organization,
                                  [...eventView.fields, {field: 'tps()'}],
                                  ['epm()', 'eps()']
                                )}
                                onSearch={(query: string) =>
                                  handleSearch(query, metricSettingState ?? undefined)
                                }
                                maxQueryLength={MAX_QUERY_LENGTH}
                              />
                            );
                          }}
                        </MEPConsumer>
                        <MetricsEventsDropdown />
                      </SearchFilterContainer>
                      {initiallyLoaded ? (
                        <TeamKeyTransactionManager.Provider
                          organization={organization}
                          teams={teams}
                          selectedTeams={['myteams']}
                          selectedProjects={eventView.project.map(String)}
                        >
                          <GenericQueryBatcher>
                            <ViewComponent {...props} />
                          </GenericQueryBatcher>
                        </TeamKeyTransactionManager.Provider>
                      ) : (
                        <LoadingIndicator />
                      )}
                    </Fragment>
                  )}
                </MEPSettingProvider>
              )}
            </MetricsDataSwitcher>
          </Layout.Main>
        </Layout.Body>
      </PageErrorProvider>
    </StyledPageContent>
  );
}

const StyledPageContent = styled(PageContent)`
  padding: 0;
`;

const StyledHeading = styled(PageHeading)`
  line-height: 40px;
`;

const SearchContainerWithFilter = styled('div')`
  display: grid;
  grid-template-rows: auto auto;
  gap: ${space(2)};
  margin-bottom: ${space(2)};

  @media (min-width: ${p => p.theme.breakpoints.small}) {
    grid-template-rows: auto;
    grid-template-columns: auto 1fr;
  }
`;

const SearchContainerWithFilterAndMetrics = styled('div')`
  display: grid;
  grid-template-rows: auto auto auto;
  gap: ${space(2)};
  margin-bottom: ${space(2)};

  @media (min-width: ${p => p.theme.breakpoints.small}) {
    grid-template-rows: auto;
    grid-template-columns: auto 1fr auto;
  }
`;
