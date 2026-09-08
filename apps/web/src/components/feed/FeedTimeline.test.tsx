import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MemoryRouter } from "react-router-dom";

import type { FeedItem } from "../../api/types";
import { FeedTimeline } from "./FeedTimeline";

function item(overrides: Partial<FeedItem>): FeedItem {
  return {
    message_id: "11111111-1111-1111-1111-111111111111",
    sent_at: "2030-01-08T07:45:00.000000Z",
    pseudonym: "resident-a",
    text: "The lift stopped again.",
    attachment_thumbnails: [],
    chorus_signal: null,
    ...overrides,
  };
}

describe("FeedTimeline", () => {
  it("shows an empty state before any reset", () => {
    render(
      <MemoryRouter>
        <FeedTimeline items={[]} />
      </MemoryRouter>,
    );
    expect(screen.getByText(/Reset the demo/)).toBeInTheDocument();
  });

  it("renders messages in order with unrelated noise unhighlighted", () => {
    const items = [
      item({ message_id: "a", text: "Package left in lobby." }),
      item({ message_id: "b", text: "Elevator stuck again." }),
    ];
    render(
      <MemoryRouter>
        <FeedTimeline items={items} />
      </MemoryRouter>,
    );
    const list = screen.getByRole("list", { name: /ambient community messages/i });
    expect(list).toBeInTheDocument();
    expect(screen.getByText("Package left in lobby.")).toBeInTheDocument();
    expect(screen.getByText("Elevator stuck again.")).toBeInTheDocument();
    expect(screen.queryByText(/chorus signal/i)).not.toBeInTheDocument();
  });

  it("shows a Chorus signal link only on linked messages", () => {
    const items = [
      item({ message_id: "a", text: "Noise message." }),
      item({
        message_id: "b",
        text: "Linked message.",
        chorus_signal: {
          candidate_case_id: "22222222-2222-2222-2222-222222222222",
          label: "Recurring lift failures",
          related_count: 3,
          status: "CANDIDATE",
        },
      }),
    ];
    render(
      <MemoryRouter>
        <FeedTimeline items={items} />
      </MemoryRouter>,
    );
    const link = screen.getByRole("link", { name: /chorus signal/i });
    expect(link).toHaveAttribute("href", "/cases/22222222-2222-2222-2222-222222222222");
  });
});
