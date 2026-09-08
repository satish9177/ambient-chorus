import type { AttachmentThumbnail as AttachmentThumbnailType } from "../../api/types";
import styles from "./AttachmentThumbnail.module.css";

/** A fixture-safe presenter preview: media type and caption only, never bytes or a raw URI. */
export function AttachmentThumbnail({ attachment }: { attachment: AttachmentThumbnailType }) {
  return (
    <figure className={styles.thumb} aria-label={`Attachment: ${attachment.media_type}`}>
      <span className={styles.icon} aria-hidden="true">
        {attachment.media_type.startsWith("image/") ? "IMG" : "FILE"}
      </span>
      <figcaption className={styles.caption}>
        {attachment.caption ?? attachment.media_type}
      </figcaption>
    </figure>
  );
}
