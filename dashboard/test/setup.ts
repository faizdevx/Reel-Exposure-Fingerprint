import '@testing-library/jest-dom/vitest';

// Times are shown in the viewer's zone; tests must not depend on the machine's.
process.env['TZ'] = 'UTC';

import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';

afterEach(cleanup);
