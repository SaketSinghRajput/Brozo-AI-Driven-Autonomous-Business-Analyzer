import { describe, expect, it } from "vitest";
import { createQueryClient, invalidateVersion, queryKeys } from "./queryClient";

describe("version-keyed cache invalidation (design.md §56)", () => {
  it("drops entries of the old version, keeps other versions and immutable evidence", () => {
    const client = createQueryClient();
    client.setQueryData(queryKeys.dashboardSpec("dsv_old"), { a: 1 });
    client.setQueryData(queryKeys.widgetRows("dsv_old", "ev_1"), [1]);
    client.setQueryData(queryKeys.capabilities("dsv_old", 3), { b: 1 });
    client.setQueryData(queryKeys.dashboardSpec("dsv_new"), { a: 2 });
    client.setQueryData(queryKeys.evidence("ev_1"), { evidence_id: "ev_1" });

    invalidateVersion(client, "dsv_old");

    expect(client.getQueryData(queryKeys.dashboardSpec("dsv_old"))).toBeUndefined();
    expect(client.getQueryData(queryKeys.widgetRows("dsv_old", "ev_1"))).toBeUndefined();
    expect(client.getQueryData(queryKeys.capabilities("dsv_old", 3))).toBeUndefined();
    expect(client.getQueryData(queryKeys.dashboardSpec("dsv_new"))).toEqual({ a: 2 });
    expect(client.getQueryData(queryKeys.evidence("ev_1"))).toEqual({ evidence_id: "ev_1" });
  });

  it("does not treat a version id prefix as a match", () => {
    const client = createQueryClient();
    client.setQueryData(queryKeys.dashboardSpec("dsv_1"), 1);
    client.setQueryData(queryKeys.dashboardSpec("dsv_10"), 10);
    invalidateVersion(client, "dsv_1");
    expect(client.getQueryData(queryKeys.dashboardSpec("dsv_10"))).toBe(10);
  });
});
