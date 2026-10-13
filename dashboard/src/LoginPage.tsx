import { useQueryClient } from '@tanstack/react-query';
import { type FormEvent, useState } from 'react';

import { ApiError, login } from './api';
import { navigate } from './router';

export function LoginPage() {
  const queryClient = useQueryClient();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPending(true);
    setError(null);
    try {
      await login(email.trim(), password);
      queryClient.clear(); // nothing cached before login belongs to this user
      navigate('/');
    } catch (err) {
      setError(
        err instanceof ApiError && err.status === 401
          ? 'Invalid email or password.'
          : 'Could not log in. Check your connection and try again.',
      );
    } finally {
      setPending(false);
    }
  }

  return (
    <main className="login">
      <h1>Log in</h1>
      <form onSubmit={(event) => void onSubmit(event)} noValidate>
        <label htmlFor="email">Email</label>
        <input
          id="email"
          name="email"
          type="email"
          autoComplete="username"
          required
          value={email}
          onChange={(event) => setEmail(event.target.value)}
        />
        <label htmlFor="password">Password</label>
        <input
          id="password"
          name="password"
          type="password"
          autoComplete="current-password"
          required
          value={password}
          onChange={(event) => setPassword(event.target.value)}
        />
        {error ? (
          <p role="alert" className="notice error">
            {error}
          </p>
        ) : null}
        <button type="submit" disabled={pending || email.trim() === '' || password === ''}>
          {pending ? 'Logging in…' : 'Log in'}
        </button>
      </form>
    </main>
  );
}
