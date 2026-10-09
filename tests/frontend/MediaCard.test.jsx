import { describe, expect, it } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { MediaCard } from "../../src/components/MediaCard.jsx";

const ITEM = {
  slug: "a-show-2024",
  type: "series",
  title: "A Show",
  year: 2024,
  country: "TH",
  posterUrl: "https://example.invalid/poster.jpg",
  releaseStatus: "Airing"
};

function renderCard(item = ITEM) {
  return render(
    <MemoryRouter>
      <MediaCard item={item} />
    </MemoryRouter>
  );
}

describe("MediaCard", () => {
  it("shows the country name, not the raw code", () => {
    renderCard();
    expect(screen.getByText(/Thailand/)).toBeInTheDocument();
    expect(screen.queryByText(/\bTH\b/)).not.toBeInTheDocument();
  });

  it("shows the poster when there is one", () => {
    renderCard();
    expect(screen.getByRole("img", { name: "A Show" })).toBeInTheDocument();
  });

  it("falls back to the placeholder when the poster fails to load", () => {
    renderCard();
    fireEvent.error(screen.getByRole("img", { name: "A Show" }));
    expect(screen.queryByRole("img", { name: "A Show" })).not.toBeInTheDocument();
    expect(screen.getByText("No poster yet")).toBeInTheDocument();
  });

  it("shows the placeholder when the title has no poster", () => {
    renderCard({ ...ITEM, posterUrl: null });
    expect(screen.getByText("No poster yet")).toBeInTheDocument();
  });
});
