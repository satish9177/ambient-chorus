import { CandidateClusterRail } from "../components/feed/CandidateClusterRail";
import { DemoResetControl } from "../components/feed/DemoResetControl";
import { FeedTimeline } from "../components/feed/FeedTimeline";
import { ErrorBanner } from "../components/shared/ErrorBanner";
import { usePersona } from "../context/PersonaContext";
import { useFeedQuery } from "../hooks/useFeed";
import { useSessionQuery } from "../hooks/useSession";

export function AmbientFeedPage() {
  const { actor } = usePersona();
  const sessionQuery = useSessionQuery(actor);
  const communityId = sessionQuery.data?.community_id ?? null;
  const feedQuery = useFeedQuery(communityId, actor);

  const items = feedQuery.data?.items ?? [];
  const hasSignal = items.some((item) => item.chorus_signal !== null);

  return (
    <section aria-labelledby="feed-heading">
      <h2 id="feed-heading">Ambient signal feed</h2>
      <p>
        Ordinary resident messages, mostly noise. Chorus links the fragments that describe the
        same recurring problem.
      </p>

      <DemoResetControl communityId={communityId} hasSignal={hasSignal} />

      {sessionQuery.isError && <ErrorBanner error={sessionQuery.error} />}
      {feedQuery.isError && <ErrorBanner error={feedQuery.error} />}

      {feedQuery.isPending && communityId && <p>Loading feed…</p>}

      <CandidateClusterRail items={items} />
      <FeedTimeline items={items} />
    </section>
  );
}
