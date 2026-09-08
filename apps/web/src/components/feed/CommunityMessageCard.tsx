import type { FeedItem } from "../../api/types";
import { AttachmentThumbnail } from "./AttachmentThumbnail";
import { ChorusSignalBadge } from "./ChorusSignalBadge";
import styles from "./CommunityMessageCard.module.css";

function formatTime(iso: string): string {
  const date = new Date(iso);
  return `${date.toLocaleDateString(undefined, { month: "short", day: "numeric" })} ${date.toLocaleTimeString(
    undefined,
    { hour: "2-digit", minute: "2-digit" },
  )}`;
}

export function CommunityMessageCard({ item }: { item: FeedItem }) {
  return (
    <li className={styles.card} data-linked={item.chorus_signal !== null}>
      <div className={styles.meta}>
        <time dateTime={item.sent_at}>{formatTime(item.sent_at)}</time>
        <span className={styles.pseudonym}>{item.pseudonym ?? "unverified sender"}</span>
      </div>
      <div className={styles.body}>
        <p className={styles.text}>{item.text}</p>
        {item.attachment_thumbnails.map((attachment) => (
          <AttachmentThumbnail key={attachment.evidence_id} attachment={attachment} />
        ))}
        {item.chorus_signal && <ChorusSignalBadge signal={item.chorus_signal} />}
      </div>
    </li>
  );
}
