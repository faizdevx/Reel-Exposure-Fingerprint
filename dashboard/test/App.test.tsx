import { QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { App } from '../src/App';
import { createQueryClient } from '../src/queryClient';

type Handler = (url: string, init?: RequestInit) => Response | Promise<Response>;

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

const SURVEY_TOPICS = [
  { topic_id: 1, slug: 'cricket', label: 'Cricket' },
  { topic_id: 2, slug: 'tech', label: 'Tech' },
  { topic_id: 3, slug: 'food', label: 'Food' },
];
const surveyDone = {
  status: 'submitted',
  completed_at: '2026-10-01T10:00:00Z',
  topics: SURVEY_TOPICS,
  ratings: { '1': 5, '2': 3, '3': 1 },
};

/** `survey` is what GET /self-report answers (already answered by default); null leaves it to `handler`. */
function stubFetch(handler: Handler, survey: unknown = surveyDone) {
  const mock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString();
    if (url === '/healthz') return Promise.resolve(json({ status: 'ok' }));
    if (url === '/api/v1/self-report' && !init?.method && survey) return Promise.resolve(json(survey));
    return Promise.resolve(handler(url, init));
  });
  vi.stubGlobal('fetch', mock);
  return mock;
}

function renderApp(path: string) {
  window.history.replaceState(null, '', path);
  return render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <App />
    </QueryClientProvider>,
  );
}

const topics = [
  { topic_id: 1, slug: 'cricket', label: 'Cricket', share: 0.5 },
  { topic_id: 2, slug: 'tech', label: 'Tech', share: 0.3 },
  { topic_id: 3, slug: 'food', label: 'Food', share: 0.2 },
];

function fingerprint(kind: string, withData = true) {
  return {
    kind,
    taken_at: withData ? '2026-10-01T12:00:00Z' : null,
    n_reels: withData ? 12 : 0,
    pipeline_version: 'fp1-x',
    session_id: null,
    topics: withData ? topics : [],
  };
}

beforeEach(() => window.history.replaceState(null, '', '/'));
afterEach(() => vi.unstubAllGlobals());

describe('dashboard page', () => {
  it('shows loading placeholders, then both fingerprints with shares that total 100%', async () => {
    stubFetch((url) => json(fingerprint(url.includes('engagement') ? 'engagement' : 'exposure')));
    renderApp('/');
    expect(screen.getAllByRole('status')).toHaveLength(1); // the survey state is read first
    await waitFor(() => expect(screen.getAllByRole('status').length).toBeGreaterThanOrEqual(3)); // two fingerprints and the prediction

    const exposure = await screen.findByRole('table', { name: 'Exposure fingerprint topic shares' });
    expect(within(exposure).getByText('50.0%')).toBeInTheDocument();
    expect(within(exposure).getByTestId('share-total')).toHaveTextContent('100.0%');
    const engagement = await screen.findByRole('table', { name: 'Engagement fingerprint topic shares' });
    expect(within(engagement).getByTestId('share-total')).toHaveTextContent('100.0%');
  });

  it('shows an empty state when there is no snapshot yet', async () => {
    stubFetch((url) => json(fingerprint(url.includes('engagement') ? 'engagement' : 'exposure', false)));
    renderApp('/');
    expect(await screen.findByText(/No exposure fingerprint yet/)).toBeInTheDocument();
    expect(screen.getByText(/No engagement fingerprint yet/)).toBeInTheDocument();
    expect(screen.queryByRole('table')).toBeNull();
  });

  it('shows an error with retry, and retry loads the data', async () => {
    let fail = true;
    stubFetch((url) => {
      if (url.includes('engagement')) return json(fingerprint('engagement'));
      return fail ? json({}, 500) : json(fingerprint('exposure'));
    });
    renderApp('/');
    const alert = await screen.findByText('Could not load the exposure fingerprint.');
    fail = false;
    await userEvent.click(within(alert.closest('[role="alert"]') as HTMLElement).getByRole('button', { name: 'Try again' }));
    expect(await screen.findByRole('table', { name: 'Exposure fingerprint topic shares' })).toBeInTheDocument();
  });

  it('sends a logged-out visitor to /login', async () => {
    stubFetch(() => json({ detail: 'Not authenticated' }, 401));
    renderApp('/');
    expect(await screen.findByRole('heading', { name: 'Log in' })).toBeInTheDocument();
    expect(window.location.pathname).toBe('/login');
  });
});

describe('sessions page', () => {
  it('lists sessions, with a clear label when a session is not processed yet', async () => {
    stubFetch(() =>
      json([
        {
          id: 'a',
          started_at: '2026-10-01T12:00:00Z',
          ended_at: '2026-10-01T12:30:00Z',
          status: 'stopped',
          stop_reason: 'user',
          n_reels: 12,
        },
        {
          id: 'b',
          started_at: '2026-10-02T12:00:00Z',
          ended_at: null,
          status: 'active',
          stop_reason: null,
          n_reels: null,
        },
      ]),
    );
    renderApp('/sessions');
    const list = await screen.findByRole('list', { name: 'Monitoring sessions' });
    const items = within(list).getAllByRole('listitem');
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveTextContent('30 min');
    expect(items[0]).toHaveTextContent('12');
    expect(items[0]).toHaveTextContent('Stopped by you');
    expect(items[1]).toHaveTextContent('not processed yet');
    expect(items[1]).toHaveTextContent('Running');
  });

  it('shows empty and error states', async () => {
    stubFetch(() => json([]));
    const first = renderApp('/sessions');
    expect(await screen.findByText(/No sessions yet/)).toBeInTheDocument();
    first.unmount();

    stubFetch(() => json({}, 500));
    renderApp('/sessions');
    expect(await screen.findByText('Could not load your sessions.')).toBeInTheDocument();
  });
});

describe('login page', () => {
  it('logs in with the CSRF header and goes to the dashboard', async () => {
    const fetchMock = stubFetch((url) => {
      if (url === '/api/v1/auth/login') return json({ id: 'u', email: 'a@example.test' });
      return json(fingerprint(url.includes('engagement') ? 'engagement' : 'exposure'));
    });
    renderApp('/login');
    await userEvent.type(screen.getByLabelText('Email'), 'a@example.test');
    await userEvent.type(screen.getByLabelText('Password'), 'correct horse battery');
    await userEvent.click(screen.getByRole('button', { name: 'Log in' }));

    await waitFor(() => expect(window.location.pathname).toBe('/'));
    const call = fetchMock.mock.calls.find(([u]) => u === '/api/v1/auth/login');
    expect(call).toBeDefined();
    const init = call?.[1] as RequestInit;
    expect(init.method).toBe('POST');
    expect((init.headers as Record<string, string>)['X-Requested-With']).toBe('ref-dashboard');
    expect(JSON.parse(init.body as string)).toEqual({
      email: 'a@example.test',
      password: 'correct horse battery',
    });
  });

  it('shows a clear message for a wrong password and stays on the page', async () => {
    stubFetch(() => json({ detail: 'Invalid email or password' }, 401));
    renderApp('/login');
    await userEvent.type(screen.getByLabelText('Email'), 'a@example.test');
    await userEvent.type(screen.getByLabelText('Password'), 'nope');
    await userEvent.click(screen.getByRole('button', { name: 'Log in' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Invalid email or password.');
    expect(window.location.pathname).toBe('/login');
  });

  it('shows a connection message when the server cannot be reached', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new TypeError('network down'))));
    renderApp('/login');
    await userEvent.type(screen.getByLabelText('Email'), 'a@example.test');
    await userEvent.type(screen.getByLabelText('Password'), 'x');
    await userEvent.click(screen.getByRole('button', { name: 'Log in' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not log in');
  });

  it('keeps the button disabled until both fields are filled', () => {
    stubFetch(() => json({}));
    renderApp('/login');
    expect(screen.getByRole('button', { name: 'Log in' })).toBeDisabled();
  });
});

describe('unknown path', () => {
  it('shows a not-found page with a way back', () => {
    stubFetch(() => json({}));
    renderApp('/nope');
    expect(screen.getByRole('heading', { name: 'Page not found' })).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------- history page

function historyBody(kind: string, bucket: string, days = 3) {
  const names = ['Cricket', 'Tech', 'Food'];
  return {
    kind,
    bucket,
    pipeline_version: 'fp1-x',
    topics: names.map((label, i) => ({ topic_id: i + 1, slug: label.toLowerCase(), label })),
    points: Array.from({ length: days }, (_, d) => {
      const lean = kind === 'exposure' ? [0.5, 0.3, 0.2] : [0.2, 0.3, 0.5];
      return {
        taken_at: `2026-10-0${d + 1}T20:00:00Z`,
        session_id: bucket === 'session' ? `s${d + 1}` : null,
        n_reels: (d + 1) * 10,
        reels_in_bucket: 10,
        shares: { '1': lean[0], '2': lean[1], '3': lean[2] },
      };
    }),
  };
}

function historyHandler(days = 3) {
  return (url: string): Response => {
    if (url.includes('/fingerprint/history')) {
      const kind = url.includes('kind=engagement') ? 'engagement' : 'exposure';
      const bucket = url.includes('bucket=session') ? 'session' : 'day';
      return json(historyBody(kind, bucket, days));
    }
    if (url.includes('/fingerprint/current')) {
      const kind = url.includes('engagement') ? 'engagement' : 'exposure';
      const lean = kind === 'exposure' ? [0.5, 0.3, 0.2] : [0.2, 0.3, 0.5];
      return json({
        ...fingerprint(kind),
        topics: ['Cricket', 'Tech', 'Food'].map((label, i) => ({
          topic_id: i + 1,
          slug: label.toLowerCase(),
          label,
          share: lean[i],
        })),
      });
    }
    if (url.includes('/sessions')) {
      return json([
        { id: 's1', started_at: '2026-10-01T19:00:00Z', ended_at: '2026-10-01T19:30:00Z', status: 'stopped', stop_reason: 'user', n_reels: 10 },
        { id: 's2', started_at: '2026-10-02T19:00:00Z', ended_at: '2026-10-02T19:20:00Z', status: 'stopped', stop_reason: 'user', n_reels: 10 },
      ]);
    }
    return json({}, 404);
  };
}

describe('history page', () => {
  it('shows the four sections with their data, every share table adding up to 100%', async () => {
    stubFetch(historyHandler());
    renderApp('/history');
    expect(await screen.findByRole('heading', { name: 'History' })).toBeInTheDocument();
    for (const name of [
      'Topic share over time',
      'Exposure versus engagement',
      'Fingerprint history',
      'Session timeline',
    ]) {
      expect(screen.getByRole('heading', { name })).toBeInTheDocument();
    }
    const table = await screen.findByRole('table', { name: 'exposure topic share by day' });
    for (const cell of within(table).getAllByTestId('row-total')) expect(cell).toHaveTextContent('100.0%');
    expect(within(table).getAllByRole('row')).toHaveLength(1 + 3);

    const compare = await screen.findByRole('table', { name: 'Exposure and engagement share by topic' });
    expect(within(compare).getByTestId('total-exposure')).toHaveTextContent('100.0%');
    expect(within(compare).getByTestId('total-engagement')).toHaveTextContent('100.0%');
    expect(screen.getByTestId('gap-summary')).toHaveTextContent('Food');
  });

  it('lists the daily snapshots newest first and the sessions oldest first', async () => {
    stubFetch(historyHandler());
    renderApp('/history');
    const snapshots = await screen.findByRole('table', { name: 'exposure daily snapshots' });
    const days = within(snapshots)
      .getAllByRole('rowheader')
      .map((c) => c.textContent);
    expect(days).toEqual(['2026-10-03', '2026-10-02', '2026-10-01']);
    const timeline = await screen.findByRole('list', { name: 'Session timeline, oldest first' });
    const items = within(timeline).getAllByRole('listitem');
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveTextContent('1 Oct');
    expect(items[1]).toHaveTextContent('2 Oct');
  });

  it('switches the fingerprint with the filter', async () => {
    stubFetch(historyHandler());
    renderApp('/history');
    await screen.findByRole('table', { name: 'exposure topic share by day' });
    await userEvent.click(screen.getByRole('radio', { name: 'Engagement' }));
    expect(await screen.findByRole('table', { name: 'engagement topic share by day' })).toBeInTheDocument();
    expect(screen.getByRole('table', { name: 'engagement daily snapshots' })).toBeInTheDocument();
  });

  it('says so when there is no history yet, in every section', async () => {
    stubFetch((url) => {
      if (url.includes('/fingerprint/history')) {
        return json({ kind: 'exposure', bucket: 'day', pipeline_version: null, topics: [], points: [] });
      }
      if (url.includes('/fingerprint/current')) {
        return json(fingerprint(url.includes('engagement') ? 'engagement' : 'exposure', false));
      }
      return json([]);
    });
    renderApp('/history');
    expect(await screen.findByText(/No history yet/)).toBeInTheDocument();
    expect(await screen.findByText(/Nothing to compare yet/)).toBeInTheDocument();
    expect(await screen.findByText(/No exposure snapshots yet/)).toBeInTheDocument();
    expect(await screen.findByText(/No sessions yet/)).toBeInTheDocument();
    expect(screen.queryByRole('table')).toBeNull();
  });

  it('draws no trend from a single day but still shows its numbers', async () => {
    stubFetch(historyHandler(1));
    renderApp('/history');
    expect(await screen.findByText(/One day so far/)).toBeInTheDocument();
    expect(await screen.findByRole('table', { name: 'exposure topic share by day' })).toBeInTheDocument();
  });

  it('shows an error with retry for a failing section only', async () => {
    let fail = true;
    stubFetch((url) => {
      if (url.includes('bucket=session') && fail) return json({}, 500);
      return historyHandler()(url);
    });
    renderApp('/history');
    const alert = await screen.findByText('Could not load the session timeline.');
    expect(await screen.findByRole('table', { name: 'exposure topic share by day' })).toBeInTheDocument();
    fail = false;
    await userEvent.click(within(alert.closest('[role="alert"]') as HTMLElement).getByRole('button', { name: 'Try again' }));
    expect(await screen.findByRole('list', { name: 'Session timeline, oldest first' })).toBeInTheDocument();
  });

  it('sends a logged-out visitor to /login', async () => {
    stubFetch(() => json({ detail: 'Not authenticated' }, 401));
    renderApp('/history');
    expect(await screen.findByRole('heading', { name: 'Log in' })).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------- topic evidence

function evidenceBody(offset: number, limit: number, total = 12) {
  const items = Array.from({ length: Math.min(limit, Math.max(0, total - offset)) }, (_, k) => {
    const i = offset + k;
    return {
      event_id: `e${i}`,
      session_id: 's1',
      reel_shortcode: `Creel${i}`,
      reel_url: `https://www.instagram.com/reel/Creel${i}/`,
      caption: i === 2 ? null : `caption ${i}`,
      caption_truncated: false,
      observed_at: '2026-10-01T18:30:00Z',
      dwell_ms: i === 3 ? null : 9000,
      video_duration_ms: 15000,
      topic_score: 0.5,
      p_text: 0.6,
      p_visual: null,
      gain: 1,
      recency: 0.9,
      contribution: 1 / (i + 2),
      share_of_topic: 0.1,
    };
  });
  const end = offset + items.length;
  return {
    topic: { id: 1, slug: 'cricket', label: 'Cricket' },
    kind: 'exposure',
    evidence_available: true,
    reason: null,
    fingerprint: {
      taken_at: '2026-10-01T12:00:00Z',
      pipeline_version: 'fp1-x',
      n_reels: 40,
      half_life_days: 7,
      topic_weight: 5,
      total_weight: 10,
      topic_share: 0.5,
    },
    explanation: {
      gain_rule: 'Every Reel counts once.',
      formula: 'contribution = topic score x gain x recency.',
      contributing_reels: total,
      contributions_sum: 5,
      coverage: 1,
    },
    total,
    limit,
    offset,
    next_offset: end < total ? end : null,
    items,
  };
}

function evidenceHandler() {
  return (url: string): Response => {
    if (url.includes('/topics/')) {
      const params = new URL(url, 'http://x').searchParams;
      return json(evidenceBody(Number(params.get('offset')), Number(params.get('limit'))));
    }
    return json(fingerprint(url.includes('engagement') ? 'engagement' : 'exposure'));
  };
}

describe('topic evidence', () => {
  it('opens from a topic row and explains the topic, with one entry per Reel', async () => {
    stubFetch(evidenceHandler());
    renderApp('/');
    const table = await screen.findByRole('table', { name: 'Exposure fingerprint topic shares' });
    await userEvent.click(within(table).getByRole('button', { name: /Why Cricket\?/ }));

    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByRole('heading', { name: 'Why Cricket is in your exposure fingerprint' })).toBeInTheDocument();
    const summary = await within(dialog).findByRole('region', { name: 'Why this topic appears' });
    expect(summary).toHaveTextContent('50.0% of your exposure fingerprint');
    expect(within(dialog).getByTestId('coverage')).toHaveTextContent('12 Reels contribute');
    const items = within(dialog).getAllByRole('listitem');
    expect(items).toHaveLength(10);
    const [firstItem] = items;
    if (!firstItem) throw new Error('no items');
    expect(within(firstItem).getByRole('link', { name: 'Open Reel Creel0' })).toHaveAttribute(
      'href',
      'https://www.instagram.com/reel/Creel0/',
    );
    expect(items[2]).toHaveTextContent('No caption was captured');
    expect(items[3]).toHaveTextContent('not recorded');
  });

  it('pages with Next and Previous', async () => {
    const fetchMock = stubFetch(evidenceHandler());
    renderApp('/');
    const table = await screen.findByRole('table', { name: 'Exposure fingerprint topic shares' });
    await userEvent.click(within(table).getByRole('button', { name: /Why Cricket\?/ }));
    const dialog = await screen.findByRole('dialog');
    expect(await within(dialog).findByTestId('range')).toHaveTextContent('Showing 1–10 of 12');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Next' }));
    await waitFor(() => expect(within(dialog).getByTestId('range')).toHaveTextContent('Showing 11–12 of 12'));
    expect(within(dialog).getByRole('button', { name: 'Next' })).toBeDisabled();
    await userEvent.click(within(dialog).getByRole('button', { name: 'Previous' }));
    await waitFor(() => expect(within(dialog).getByTestId('range')).toHaveTextContent('Showing 1–10 of 12'));
    const asked = fetchMock.mock.calls.map(([u]) => String(u)).filter((u) => u.includes('/topics/'));
    expect(asked.some((u) => u.includes('offset=10') && u.includes('limit=10'))).toBe(true);
    expect(asked.every((u) => u.startsWith('/api/v1/topics/1/evidence?kind=exposure'))).toBe(true);
  });

  it('closes with Escape and returns focus to the topic', async () => {
    stubFetch(evidenceHandler());
    renderApp('/');
    const table = await screen.findByRole('table', { name: 'Exposure fingerprint topic shares' });
    const opener = within(table).getByRole('button', { name: /Why Cricket\?/ });
    await userEvent.click(opener);
    await screen.findByRole('dialog');
    await userEvent.keyboard('{Escape}');
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(opener).toHaveFocus();
  });

  it('explains an empty answer, and an error offers a retry', async () => {
    stubFetch((url) => {
      if (url.includes('/topics/')) {
        return json({
          ...evidenceBody(0, 10),
          evidence_available: false,
          reason: 'topic_not_in_fingerprint',
          explanation: null,
          items: [],
          total: 0,
        });
      }
      return json(fingerprint(url.includes('engagement') ? 'engagement' : 'exposure'));
    });
    renderApp('/');
    const table = await screen.findByRole('table', { name: 'Exposure fingerprint topic shares' });
    await userEvent.click(within(table).getByRole('button', { name: /Why Cricket\?/ }));
    expect(await screen.findByText(/No Reel has put any weight on this topic/)).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------- deletion

const deletionReport = (scope: 'session' | 'all', remaining: Record<string, number> = {}) => ({
  scope,
  existed: true,
  deleted: { sessions: 1, reel_exposures: 5 },
  remaining: { sessions: 0, reel_exposures: 0, frame_files: 0, ...remaining },
  frame_files_deleted: 4,
  fingerprints_rebuilt: 1,
  fingerprints_dropped: 0,
  verified: Object.values(remaining).every((v) => v === 0),
});

describe('deleting data', () => {
  const oneSession = {
    id: 's1',
    started_at: '2026-10-01T12:00:00Z',
    ended_at: '2026-10-01T12:20:00Z',
    status: 'stopped',
    stop_reason: 'user',
    n_reels: 5,
  };

  it('deletes a session after a confirmation, sending the CSRF header, then shows the verified result', async () => {
    let sessions = [oneSession];
    const fetchMock = stubFetch((url, init) => {
      if (url === '/api/v1/sessions/s1' && init?.method === 'DELETE') {
        sessions = [];
        return json(deletionReport('session'));
      }
      return json(sessions);
    });
    renderApp('/sessions');
    await userEvent.click(await screen.findByRole('button', { name: /Delete the session of/ }));
    const confirm = screen.getByRole('group', { name: 'Confirm deleting this session' });
    expect(confirm).toHaveTextContent('its 5 Reels');
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'DELETE')).toBe(false);

    await userEvent.click(within(confirm).getByRole('button', { name: 'Yes, delete this session' }));

    expect(await screen.findByText(/No sessions yet/)).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('Verified: nothing left in any table or on disk');
    const call = fetchMock.mock.calls.find(([, init]) => init?.method === 'DELETE');
    expect((call?.[1]?.headers as Record<string, string>)['X-Requested-With']).toBe('ref-dashboard');
  });

  it('shows an unfinished deletion as unfinished, with what is left', async () => {
    stubFetch((url, init) => {
      if (init?.method === 'DELETE') return json({ detail: 'deletion_incomplete', ...deletionReport('session', { frame_files: 2 }) }, 500);
      return json([oneSession]);
    });
    renderApp('/sessions');
    await userEvent.click(await screen.findByRole('button', { name: /Delete the session of/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Yes, delete this session' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('not finished: 2 stored frame files is still there');
    expect(screen.queryByText(/Verified:/)).toBeNull();
    expect(screen.getAllByRole('listitem')).toHaveLength(1); // still listed: not gone
  });

  it('keeps "Delete all my data" locked until DELETE is typed, then deletes and confirms', async () => {
    const fetchMock = stubFetch((url, init) => {
      if (url === '/api/v1/me/data' && init?.method === 'DELETE') return json(deletionReport('all'));
      return json({});
    });
    renderApp('/account');
    const button = screen.getByRole('button', { name: 'Delete all my data' });
    expect(button).toBeDisabled();
    await userEvent.type(screen.getByLabelText(/Type DELETE/), 'DELET');
    expect(button).toBeDisabled();
    await userEvent.type(screen.getByLabelText(/Type DELETE/), 'E');
    expect(button).toBeEnabled();
    await userEvent.click(button);
    expect(await screen.findByRole('heading', { name: 'Your data is deleted' })).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('Verified: nothing left in any table or on disk');
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === 'DELETE')).toHaveLength(1);
  });

  it('treats a 401 on delete-all as "already gone" and goes to the login page', async () => {
    stubFetch((_url, init) => (init?.method === 'DELETE' ? json({ detail: 'Not authenticated' }, 401) : json({})));
    renderApp('/account');
    await userEvent.type(screen.getByLabelText(/Type DELETE/), 'DELETE');
    await userEvent.click(screen.getByRole('button', { name: 'Delete all my data' }));
    expect(await screen.findByRole('heading', { name: 'Log in' })).toBeInTheDocument();
  });
});


// ---------------------------------------------------------------- prediction panel

function figures(top1: number, n = 100) {
  return { n, top1, top3: 0.9, log_loss: 1.234 };
}

function predictionBody(over: Record<string, unknown> = {}, evaluationOver: Record<string, unknown> = {}) {
  const topic = (id: number, label: string, probability: number) => ({ topic_id: id, slug: label.toLowerCase(), label, probability });
  const model = (name: string) => ({
    model: name,
    name,
    top: [topic(1, 'Cricket', 0.5), topic(2, 'Tech', 0.3), topic(3, 'Food', 0.1)],
    rest_probability: 0.1,
  });
  const models = Object.fromEntries(
    ['uniform', 'most_frequent', 'decayed_fingerprint', 'markov', 'hybrid'].map((m) => [
      m,
      { all_time: figures(0.4, 300), rolling: figures(0.5) },
    ]),
  );
  return {
    label: 'Predicted next observed topic',
    note: 'A statistical guess at the topic of the next Reel the feed shows you. It is not a prediction of what you like.',
    available: true,
    reason: null,
    pipeline_version: 'pr1-x',
    as_of: '2026-10-09T10:00:00Z',
    based_on_reels: 300,
    last_topic: topic(2, 'Tech', 1),
    backoff: false,
    row_observations: 41,
    min_row_observations: 20,
    hybrid_lambda: 0.3,
    hybrid: model('hybrid'),
    models: [model('uniform'), model('hybrid')],
    evaluation: {
      scored_reels: 300,
      required_reels: 1500,
      complete: false,
      status: 'not complete: 300 of 1500 Reels scored',
      rolling_window: 100,
      models,
      comparisons: null,
      hybrid_beats_required_baselines: null,
      ...evaluationOver,
    },
    ...over,
  };
}

const interval = (estimate: number, low: number | null, high: number | null) => ({ estimate, low, high });

describe('prediction panel', () => {
  const handler = (body: unknown) => (url: string): Response =>
    url.includes('/predictions/next') ? json(body) : json(fingerprint(url.includes('engagement') ? 'engagement' : 'exposure'));

  it('is named "Predicted next observed topic" and its probabilities add up to 100%', async () => {
    stubFetch(handler(predictionBody()));
    renderApp('/');
    expect(await screen.findByRole('heading', { name: 'Predicted next observed topic' })).toBeInTheDocument();
    const table = await screen.findByRole('table', { name: 'Predicted next observed topic, combined model' });
    expect(within(table).getByTestId('prediction-total')).toHaveTextContent('100.0%');
    expect(within(table).getByText('All other topics')).toBeInTheDocument();
    expect(screen.getByText(/not a prediction of what you like/)).toBeInTheDocument();
  });

  it('says the evaluation is not complete and claims nothing before then', async () => {
    stubFetch(handler(predictionBody()));
    renderApp('/');
    const banner = await screen.findByTestId('evaluation-banner');
    expect(banner).toHaveTextContent('Evaluation not complete: 300 of 1,500 Reels scored');
    expect(banner).toHaveTextContent('preliminary');
    const text = document.body.textContent?.toLowerCase() ?? '';
    for (const word of ['success', 'accurate', 'outperform', 'beats', 'you prefer', 'your preference']) {
      expect(text).not.toContain(word);
    }
    const table = screen.getByRole('table', { name: 'How earlier predictions scored' });
    expect(within(table).getAllByRole('row')).toHaveLength(1 + 5);
    expect(within(table).getByRole('row', { name: /Your most frequent topics/ })).toHaveTextContent('50.0%');
  });

  it('explains the fingerprint standing in for too few transitions', async () => {
    stubFetch(handler(predictionBody({ backoff: true, row_observations: 7 })));
    renderApp('/');
    expect(await screen.findByTestId('prediction-basis')).toHaveTextContent(
      'After Tech you have 7 recorded follow-ups, fewer than the 20 needed, so your fingerprint is used instead.',
    );
  });

  it('states the comparison with intervals only once complete, and says when the baselines were not beaten', async () => {
    const comparison = (verdict: string) => ({
      log_loss_difference: interval(-0.05, -0.1, 0.02),
      top1_difference: interval(0.01, -0.02, 0.04),
      log_loss_verdict: verdict,
    });
    stubFetch(
      handler(
        predictionBody(
          {},
          {
            scored_reels: 1600,
            complete: true,
            status: 'complete',
            comparisons: {
              uniform: comparison('hybrid_lower'),
              most_frequent: comparison('no_clear_difference'),
              decayed_fingerprint: comparison('no_clear_difference'),
              markov: comparison('hybrid_higher'),
            },
            hybrid_beats_required_baselines: false,
          },
        ),
      ),
    );
    renderApp('/');
    const banner = await screen.findByTestId('evaluation-banner');
    expect(banner).toHaveTextContent('Evaluation complete: 1600 Reels scored');
    expect(banner).toHaveTextContent('was not clearly lower than both required baselines');
    expect(banner).toHaveTextContent('Combined vs your most frequent topics: log loss difference -0.050 (95% interval -0.100 to 0.020): no clear difference');
    expect(banner).toHaveTextContent('Combined vs every topic equally likely');
    expect(banner).toHaveTextContent(': lower');
    expect(banner).toHaveTextContent(': higher');
  });

  it('shows an empty state before any prediction exists', async () => {
    stubFetch(handler({ ...predictionBody(), available: false, reason: 'no_predictions', hybrid: null, models: [], evaluation: null }));
    renderApp('/');
    expect(await screen.findByText(/No predictions yet/)).toBeInTheDocument();
  });

  it('shows an error with a retry', async () => {
    let fail = true;
    stubFetch((url) => {
      if (url.includes('/predictions/next')) return fail ? json({}, 500) : json(predictionBody());
      return json(fingerprint(url.includes('engagement') ? 'engagement' : 'exposure'));
    });
    renderApp('/');
    const alert = await screen.findByText('Could not load the prediction.');
    fail = false;
    await userEvent.click(within(alert.closest('[role="alert"]') as HTMLElement).getByRole('button', { name: 'Try again' }));
    expect(await screen.findByRole('table', { name: 'How earlier predictions scored' })).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------- self-report survey

describe('self-report survey', () => {
  const pending = { ...surveyDone, status: 'pending', completed_at: null, ratings: {} };

  it('comes before the fingerprints, which are not even requested while it is pending', async () => {
    const mock = stubFetch(() => json({}), pending);
    renderApp('/');
    expect(await screen.findByRole('heading', { name: 'Before you see your fingerprints' })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Your fingerprints' })).not.toBeInTheDocument();
    const urls = mock.mock.calls.map((c) => String(c[0]));
    expect(urls.some((u) => u.includes('/fingerprint') || u.includes('/predictions'))).toBe(false);
  });

  it('also guards the history page', async () => {
    stubFetch(() => json({}), pending);
    renderApp('/history');
    expect(await screen.findByRole('heading', { name: 'Before you see your fingerprints' })).toBeInTheDocument();
  });

  it('needs every topic rated before it can be saved', async () => {
    stubFetch(() => json({}), pending);
    renderApp('/');
    const save = await screen.findByRole('button', { name: 'Save my ratings' });
    expect(save).toBeDisabled();
    expect(screen.getByText('0 of 3 topics rated.')).toBeInTheDocument();
    await userEvent.click(within(screen.getByRole('group', { name: 'Cricket' })).getByRole('radio', { name: '5' }));
    await userEvent.click(within(screen.getByRole('group', { name: 'Tech' })).getByRole('radio', { name: '3' }));
    expect(save).toBeDisabled();
    expect(screen.getByText('2 of 3 topics rated.')).toBeInTheDocument();
    await userEvent.click(within(screen.getByRole('group', { name: 'Food' })).getByRole('radio', { name: '1' }));
    expect(save).toBeEnabled();
  });

  it('sends the ratings with the CSRF header, then shows the dashboard', async () => {
    const calls: { method: string; headers: Record<string, string>; body: string }[] = [];
    let answered = false;
    stubFetch((url, init) => {
      if (url === '/api/v1/self-report' && init?.method === 'PUT') {
        calls.push({ method: 'PUT', headers: init.headers as Record<string, string>, body: String(init.body) });
        answered = true;
        return json(surveyDone);
      }
      return json(fingerprint(url.includes('engagement') ? 'engagement' : 'exposure'));
    }, null);
    // GET answers by state: pending until saved
    const original = globalThis.fetch;
    vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url === '/api/v1/self-report' && !init?.method) return Promise.resolve(json(answered ? surveyDone : pending));
      return original(input, init);
    });
    renderApp('/');
    for (const [name, value] of [['Cricket', '5'], ['Tech', '3'], ['Food', '1']] as const) {
      await userEvent.click(within(await screen.findByRole('group', { name })).getByRole('radio', { name: value }));
    }
    await userEvent.click(screen.getByRole('button', { name: 'Save my ratings' }));
    expect(await screen.findByRole('heading', { name: 'Your fingerprints' })).toBeInTheDocument();
    expect(calls).toHaveLength(1);
    expect(calls[0]?.headers['X-Requested-With']).toBe('ref-dashboard');
    expect(JSON.parse(calls[0]?.body ?? '{}')).toEqual({ ratings: { '1': 5, '2': 3, '3': 1 } });
  });

  it('can be skipped', async () => {
    let skipped = false;
    stubFetch((url) => json(fingerprint(url.includes('engagement') ? 'engagement' : 'exposure')), null);
    const original = globalThis.fetch;
    vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url === '/api/v1/self-report/skip') {
        skipped = true;
        return Promise.resolve(json({ ...surveyDone, status: 'skipped', ratings: {} }));
      }
      if (url === '/api/v1/self-report') return Promise.resolve(json(skipped ? { ...surveyDone, status: 'skipped', ratings: {} } : pending));
      return original(input, init);
    });
    renderApp('/');
    await userEvent.click(await screen.findByRole('button', { name: 'Skip this' }));
    expect(await screen.findByRole('heading', { name: 'Your fingerprints' })).toBeInTheDocument();
    expect(skipped).toBe(true);
  });

  it('keeps the form and says so when saving fails', async () => {
    stubFetch((url, init) => (init?.method === 'PUT' ? json({}, 500) : json({})), pending);
    renderApp('/');
    for (const name of ['Cricket', 'Tech', 'Food']) {
      await userEvent.click(within(await screen.findByRole('group', { name })).getByRole('radio', { name: '4' }));
    }
    await userEvent.click(screen.getByRole('button', { name: 'Save my ratings' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not save your answers');
    expect(screen.getByRole('button', { name: 'Save my ratings' })).toBeEnabled();
  });

  it('is not shown again once answered', async () => {
    stubFetch((url) => json(fingerprint(url.includes('engagement') ? 'engagement' : 'exposure')));
    renderApp('/');
    expect(await screen.findByRole('heading', { name: 'Your fingerprints' })).toBeInTheDocument();
    expect(screen.queryByText('Before you see your fingerprints')).not.toBeInTheDocument();
  });
});
