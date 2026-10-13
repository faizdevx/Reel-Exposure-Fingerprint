import { useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';

import { ApiError, deleteAllData, type DeletionReport } from './api';
import { DeletionFailure, DeletionResult } from './DeletionNotice';
import { navigate } from './router';

export const CONFIRM_WORD = 'DELETE';

/** The one place to erase everything: what goes, a typed confirmation, then a verified result. */
export function AccountPage() {
  const queryClient = useQueryClient();
  const [typed, setTyped] = useState('');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [done, setDone] = useState<DeletionReport | null>(null);

  async function run() {
    setPending(true);
    setError(null);
    try {
      const report = await deleteAllData();
      queryClient.clear();
      setDone(report);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        // The account is already gone (or the login ended): nothing is left to delete.
        queryClient.clear();
        navigate('/login', true);
        return;
      }
      setError(err);
    } finally {
      setPending(false);
    }
  }

  if (done) {
    return (
      <>
        <h1>Your data is deleted</h1>
        <DeletionResult report={done} />
        <p>
          Your account, sessions, Reels, fingerprints and stored frames are gone, and the extension
          no longer has access. <a href="/login">Back to the login page</a>
        </p>
      </>
    );
  }

  return (
    <>
      <h1>Account and data</h1>
      <section className="card block" aria-labelledby="delete-heading">
        <h2 id="delete-heading">Delete all my data</h2>
        <p>This permanently removes:</p>
        <ul>
          <li>every monitoring session and every Reel recorded in it,</li>
          <li>the topics assigned to those Reels, your fingerprints and their history,</li>
          <li>predictions, processing jobs and the stored frames,</li>
          <li>your consent record, extension tokens and your account itself.</li>
        </ul>
        <p>
          Afterwards the server checks every table and the frame folder, and tells you if anything is
          left. This cannot be undone.
        </p>
        <label htmlFor="confirm">
          Type <strong>{CONFIRM_WORD}</strong> to confirm
        </label>
        <input
          id="confirm"
          autoComplete="off"
          value={typed}
          onChange={(event) => setTyped(event.target.value)}
        />
        {error ? <DeletionFailure error={error} onRetry={() => void run()} /> : null}
        <button
          type="button"
          className="danger"
          disabled={typed !== CONFIRM_WORD || pending}
          onClick={() => void run()}
        >
          {pending ? 'Deleting…' : 'Delete all my data'}
        </button>
      </section>
    </>
  );
}
