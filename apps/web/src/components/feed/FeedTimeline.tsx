import type { FeedItem } from "../../api/types";
import { CommunityMessageCard } from "./CommunityMessageCard";
import styles from "./FeedTimeline.module.css";

export function FeedTimeline({ items }: { items: FeedItem[] }) {
  if (items.length === 0) {
    return (
      <div className={styles.empty}>
        No messages yet. Reset the demo to seed the ambient feed.
      </div>
    );
  }

  return (
    <ol className={styles.list} aria-label="Ambient community messages">
      {items.map((item) => (
        <CommunityMessageCard key={item.message_id} item={item} />
      ))}
    </ol>
  );
}
