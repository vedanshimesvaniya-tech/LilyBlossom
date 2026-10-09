import { describe, expect, it } from "vitest";
import { getAiring, getTitlesByType, getUpcoming } from "../../src/lib/catalogQueries.js";

// A tiny stand in for the Supabase query builder. It records each call
// so a test can check which filters and sort options a query used.
function makeFakeSupabase() {
  const calls = [];
  const builder = {
    select: (...args) => (calls.push(["select", ...args]), builder),
    eq: (...args) => (calls.push(["eq", ...args]), builder),
    in: (...args) => (calls.push(["in", ...args]), builder),
    or: (...args) => (calls.push(["or", ...args]), builder),
    order: (...args) => (calls.push(["order", ...args]), builder),
    limit: (...args) => (calls.push(["limit", ...args]), builder),
    range: (...args) => (calls.push(["range", ...args]), builder),
    then: (resolve) => resolve({ data: [], count: 0, error: null })
  };
  return { calls, from: () => builder };
}

function orderCall(calls) {
  return calls.find(([name]) => name === "order");
}

describe("catalog queries", () => {
  it("puts titles with no year last when sorting Newest", async () => {
    const fake = makeFakeSupabase();
    await getTitlesByType(fake, "series", { sort: "Newest" });
    expect(orderCall(fake.calls)).toEqual(["order", "release_year", { ascending: false, nullsFirst: false }]);
  });

  it("puts titles with no year last when sorting Oldest", async () => {
    const fake = makeFakeSupabase();
    await getTitlesByType(fake, "series", { sort: "Oldest" });
    expect(orderCall(fake.calls)).toEqual(["order", "release_year", { ascending: true, nullsFirst: false }]);
  });

  it("does not add a nulls option for an A-Z sort", async () => {
    const fake = makeFakeSupabase();
    await getTitlesByType(fake, "series", { sort: "A-Z" });
    expect(orderCall(fake.calls)).toEqual(["order", "canonical_title", { ascending: true }]);
  });

  it("puts airing titles with no year last", async () => {
    const fake = makeFakeSupabase();
    await getAiring(fake);
    expect(orderCall(fake.calls)).toEqual(["order", "release_year", { ascending: false, nullsFirst: false }]);
  });

  it("lists all three not yet released statuses on the Upcoming page", async () => {
    const fake = makeFakeSupabase();
    await getUpcoming(fake);
    const statusFilter = fake.calls.find(([name]) => name === "in");
    expect(statusFilter).toEqual(["in", "release_status", ["Announced", "In Production", "Upcoming"]]);
  });

  it("hides titles whose release date has already passed, but keeps undated ones", async () => {
    const fake = makeFakeSupabase();
    await getUpcoming(fake);
    const dateFilter = fake.calls.find(([name]) => name === "or");
    const today = new Date().toISOString().slice(0, 10);
    expect(dateFilter).toEqual(["or", `release_date.is.null,release_date.gte.${today}`]);
  });
});
