import { QueryCache, QueryClient } from '@tanstack/react-query';

import { ApiError } from './api';
import { navigate } from './router';

/**
 * One place decides what "not logged in" means: any query that answers 401 sends the user to
 * /login. Other errors stay with the component, which shows them and offers a retry.
 */
export function createQueryClient(options: { retry?: boolean } = {}): QueryClient {
  return new QueryClient({
    queryCache: new QueryCache({
      onError: (error) => {
        if (error instanceof ApiError && error.status === 401) navigate('/login', true);
      },
    }),
    defaultOptions: {
      queries: {
        // A 401 or other 4xx will not change by asking again; a dropped connection might.
        retry:
          options.retry === false
            ? false
            : (count, error) => {
                if (error instanceof ApiError && error.status < 500) return false;
                return count < 2;
              },
        refetchOnWindowFocus: false,
      },
    },
  });
}
