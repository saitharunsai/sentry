import {SeriesApi} from 'sentry/types';

export enum Outcome {
  ACCEPTED = 'accepted',
  FILTERED = 'filtered',
  INVALID = 'invalid',
  DROPPED = 'dropped', // this is not a real outcome coming from the server
  RATE_LIMITED = 'rate_limited',
  CLIENT_DISCARD = 'client_discard',
}

/**
 * Raw response from API endpoint
 */
export interface UsageSeries extends SeriesApi {
  // index signature is present because we often send this
  // data to sentry as part of the event context.
  end: string;
  start: string;
}

export type UsageStat = {
  accepted: number;
  date: string;
  dropped: {
    total: number;
    other?: number;
  };
  filtered: number;
  total: number;
};
