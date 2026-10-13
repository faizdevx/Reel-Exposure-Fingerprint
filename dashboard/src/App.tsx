import { AccountPage } from './AccountPage';
import { FingerprintCard } from './FingerprintCard';
import { HistoryPage } from './HistoryPage';
import { Layout } from './Layout';
import { LoginPage } from './LoginPage';
import { PredictionPanel } from './PredictionPanel';
import { routeFor, usePathname } from './router';
import { SessionList } from './SessionList';
import { SurveyGate } from './SurveyGate';

function DashboardPage() {
  return (
    <>
      <h1>Your fingerprints</h1>
      <p className="lead">
        Each fingerprint splits 100% across topics. Exposure is what the feed showed you; engagement
        weights topics by how long you stayed on them.
      </p>
      <div className="grid">
        <FingerprintCard
          kind="exposure"
          title="Exposure fingerprint"
          blurb="What Instagram showed you, topic by topic."
          color="var(--exposure)"
        />
        <FingerprintCard
          kind="engagement"
          title="Engagement fingerprint"
          blurb="What you spent time on. Skips count for little, long views for more."
          color="var(--engagement)"
        />
      </div>
      <PredictionPanel />
    </>
  );
}

function SessionsPage() {
  return (
    <>
      <h1>Sessions</h1>
      <SessionList />
    </>
  );
}

function NotFound() {
  return (
    <>
      <h1>Page not found</h1>
      <p>
        <a href="/">Back to the dashboard</a>
      </p>
    </>
  );
}

export function App() {
  const pathname = usePathname();
  const route = routeFor(pathname);
  if (route === '/login') return <LoginPage />;
  return (
    <Layout path={route}>
      {route === '/' ? (
        <SurveyGate>
          <DashboardPage />
        </SurveyGate>
      ) : route === '/sessions' ? (
        <SessionsPage />
      ) : route === '/history' ? (
        <SurveyGate>
          <HistoryPage />
        </SurveyGate>
      ) : route === '/account' ? (
        <AccountPage />
      ) : (
        <NotFound />
      )}
    </Layout>
  );
}
