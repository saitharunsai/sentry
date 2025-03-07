import styled from '@emotion/styled';
import {LocationDescriptor} from 'history';

import {KeyValueTableRow} from 'sentry/components/keyValueTable';
import Link from 'sentry/components/links/link';
import Tooltip from 'sentry/components/tooltip';
import Version from 'sentry/components/version';
import {t} from 'sentry/locale';
import {EventTag} from 'sentry/types/event';

import AnnotatedText from './events/meta/annotatedText';

interface Props {
  generateUrl: (tag: EventTag) => LocationDescriptor;
  query: string;
  tag: EventTag;
  meta?: Record<any, any>;
}

function TagsTableRow({tag, query, generateUrl, meta}: Props) {
  const tagInQuery = query.includes(`${tag.key}:`);
  const target = tagInQuery ? undefined : generateUrl(tag);
  const keyMetaData = meta?.key?.[''];
  const valueMetaData = meta?.value?.[''];

  const renderTagValue = () => {
    switch (tag.key) {
      case 'release':
        return <Version version={tag.value} anchor={false} withPackage />;
      default:
        return tag.value;
    }
  };
  return (
    <KeyValueTableRow
      keyName={
        !!keyMetaData && !tag.key ? (
          <AnnotatedText value={tag.key} meta={keyMetaData} />
        ) : (
          <StyledTooltip title={tag.key}>{tag.key}</StyledTooltip>
        )
      }
      value={
        !!valueMetaData && !tag.value ? (
          <AnnotatedText value={tag.value} meta={valueMetaData} />
        ) : keyMetaData?.err?.length ? (
          <span>{renderTagValue()}</span>
        ) : tagInQuery ? (
          <Tooltip title={t('This tag is in the current filter conditions')}>
            <span>{renderTagValue()}</span>
          </Tooltip>
        ) : (
          <StyledTooltip title={renderTagValue()}>
            <Link to={target || ''}>{renderTagValue()}</Link>
          </StyledTooltip>
        )
      }
    />
  );
}

export default TagsTableRow;

const StyledTooltip = styled(Tooltip)`
  ${p => p.theme.overflowEllipsis};
`;
