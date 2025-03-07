import {mountWithTheme} from 'sentry-test/enzyme';
import {render, screen, userEvent} from 'sentry-test/reactTestingLibrary';

import {openModal} from 'sentry/actionCreators/modal';
import DataScrubbing from 'sentry/views/settings/components/dataScrubbing';
import {ProjectId} from 'sentry/views/settings/components/dataScrubbing/types';

jest.mock('sentry/actionCreators/modal');

const relayPiiConfig = TestStubs.DataScrubbingRelayPiiConfig();
const stringRelayPiiConfig = JSON.stringify(relayPiiConfig);
const organizationSlug = 'sentry';
const handleUpdateOrganization = jest.fn();
const additionalContext = 'These rules can be configured for each project.';

jest.mock('sentry/actionCreators/indicator');

function getOrganization(piiConfig?: string) {
  return TestStubs.Organization(
    piiConfig ? {id: '123', relayPiiConfig: piiConfig} : {id: '123'}
  );
}

function renderComponent({
  disabled,
  projectId,
  endpoint,
  ...props
}: Partial<Omit<DataScrubbing<ProjectId>['props'], 'endpoint'>> &
  Pick<DataScrubbing<ProjectId>['props'], 'endpoint'>) {
  const organization = props.organization ?? getOrganization();
  if (projectId) {
    return mountWithTheme(
      <DataScrubbing
        additionalContext={additionalContext}
        endpoint={endpoint}
        projectId={projectId}
        relayPiiConfig={stringRelayPiiConfig}
        disabled={disabled}
        organization={organization}
        onSubmitSuccess={handleUpdateOrganization}
      />
    );
  }

  return mountWithTheme(
    <DataScrubbing
      additionalContext={additionalContext}
      endpoint={endpoint}
      relayPiiConfig={stringRelayPiiConfig}
      disabled={disabled}
      organization={organization}
      onSubmitSuccess={handleUpdateOrganization}
    />
  );
}

describe('Data Scrubbing', () => {
  describe('Organization level', () => {
    const endpoint = `organization/${organizationSlug}/`;

    it('default render', () => {
      const wrapper = renderComponent({disabled: false, endpoint});

      // PanelHeader
      expect(wrapper.find('PanelHeader').text()).toEqual('Advanced Data Scrubbing');

      // PanelAlert
      const panelAlert = wrapper.find('PanelAlert');
      expect(panelAlert.text()).toEqual(
        `${additionalContext} The new rules will only apply to upcoming events.  For more details, see full documentation on data scrubbing.`
      );

      const readDocsLink = panelAlert.find('a');
      expect(readDocsLink.text()).toEqual('full documentation on data scrubbing');
      expect(readDocsLink.prop('href')).toEqual(
        'https://docs.sentry.io/product/data-management-settings/scrubbing/advanced-datascrubbing/'
      );

      // PanelBody
      const panelBody = wrapper.find('PanelBody');
      expect(panelBody).toHaveLength(1);
      expect(panelBody.find('ListItem')).toHaveLength(3);

      // OrganizationRules
      const organizationRules = panelBody.find('OrganizationRules');
      expect(organizationRules).toHaveLength(0);

      // PanelAction
      const actionButtons = wrapper.find('PanelAction').find('Button');
      expect(actionButtons).toHaveLength(2);
      expect(actionButtons.at(0).text()).toEqual('Read Docs');
      expect(actionButtons.at(1).text()).toEqual('Add Rule');
      expect(actionButtons.at(1).prop('disabled')).toEqual(false);
    });

    it('render disabled', () => {
      const wrapper = renderComponent({disabled: true, endpoint});

      // PanelBody
      const panelBody = wrapper.find('PanelBody');
      expect(panelBody).toHaveLength(1);
      expect(panelBody.find('List').prop('isDisabled')).toEqual(true);

      // PanelAction
      const actionButtons = wrapper.find('PanelAction').find('StyledButton');
      expect(actionButtons).toHaveLength(2);
      expect(actionButtons.at(0).prop('disabled')).toEqual(false);
      expect(actionButtons.at(1).prop('disabled')).toEqual(true);
    });
  });

  describe('Project level', () => {
    const projectId = 'foo';
    const endpoint = `/projects/${organizationSlug}/${projectId}/`;

    it('default render', () => {
      const wrapper = renderComponent({
        disabled: false,
        projectId,
        endpoint,
      });

      // PanelHeader
      expect(wrapper.find('PanelHeader').text()).toEqual('Advanced Data Scrubbing');

      // PanelAlert
      const panelAlert = wrapper.find('PanelAlert');
      expect(panelAlert.text()).toEqual(
        `${additionalContext} The new rules will only apply to upcoming events.  For more details, see full documentation on data scrubbing.`
      );

      const readDocsLink = panelAlert.find('a');
      expect(readDocsLink.text()).toEqual('full documentation on data scrubbing');
      expect(readDocsLink.prop('href')).toEqual(
        'https://docs.sentry.io/product/data-management-settings/scrubbing/advanced-datascrubbing/'
      );

      // PanelBody
      const panelBody = wrapper.find('PanelBody');
      expect(panelBody).toHaveLength(1);
      expect(panelBody.find('ListItem')).toHaveLength(3);

      // OrganizationRules
      const organizationRules = panelBody.find('OrganizationRules');
      expect(organizationRules).toHaveLength(1);
      expect(organizationRules.text()).toEqual(
        'There are no data scrubbing rules at the organization level'
      );

      // PanelAction
      const actionButtons = wrapper.find('PanelAction').find('Button');
      expect(actionButtons).toHaveLength(2);
      expect(actionButtons.at(0).text()).toEqual('Read Docs');
      expect(actionButtons.at(1).text()).toEqual('Add Rule');
      expect(actionButtons.at(1).prop('disabled')).toEqual(false);
    });

    it('render disabled', () => {
      const wrapper = renderComponent({disabled: true, endpoint});

      // PanelBody
      const panelBody = wrapper.find('PanelBody');
      expect(panelBody).toHaveLength(1);
      expect(panelBody.find('List').prop('isDisabled')).toEqual(true);

      // PanelAction
      const actionButtons = wrapper.find('PanelAction').find('StyledButton');
      expect(actionButtons).toHaveLength(2);
      expect(actionButtons.at(0).prop('disabled')).toEqual(false);
      expect(actionButtons.at(1).prop('disabled')).toEqual(true);
    });

    it('OrganizationRules has content', () => {
      const wrapper = renderComponent({
        disabled: false,
        organization: getOrganization(stringRelayPiiConfig),
        projectId,
        endpoint,
      });

      // OrganizationRules
      const organizationRules = wrapper.find('OrganizationRules');
      expect(organizationRules).toHaveLength(1);
      expect(organizationRules.find('Header').text()).toEqual('Organization Rules');
      const listItems = organizationRules.find('ListItem');
      expect(listItems).toHaveLength(3);
      expect(listItems.at(0).find('[role="button"]')).toHaveLength(0);
    });

    it('Delete rule successfully', () => {
      // TODO(test): Improve this test by checking if the confirmation dialog is opened
      render(
        <DataScrubbing
          additionalContext={additionalContext}
          endpoint={endpoint}
          projectId={projectId}
          relayPiiConfig={stringRelayPiiConfig}
          disabled={false}
          organization={getOrganization()}
          onSubmitSuccess={handleUpdateOrganization}
        />
      );

      userEvent.click(screen.getAllByLabelText('Delete Rule')[0]);

      expect(openModal).toHaveBeenCalled();
    });

    it('Open Add Rule Modal', () => {
      const wrapper = renderComponent({
        disabled: false,
        projectId,
        endpoint,
      });

      const addbutton = wrapper
        .find('PanelAction')
        .find('[aria-label="Add Rule"]')
        .hostNodes();

      addbutton.simulate('click');

      expect(openModal).toHaveBeenCalled();
    });

    it('Open Edit Rule Modal', () => {
      const wrapper = renderComponent({
        disabled: false,
        projectId,
        endpoint,
      });

      const editButton = wrapper
        .find('PanelBody')
        .find('[aria-label="Edit Rule"]')
        .hostNodes();

      editButton.at(0).simulate('click');

      expect(openModal).toHaveBeenCalled();
    });
  });
});
