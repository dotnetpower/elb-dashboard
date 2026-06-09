/**
 * Tests for the peering health classifier — locks in the orphan/ghost
 * detection that drives the "this VNet no longer exists, delete it" UI.
 */
import { describe, expect, it } from "vitest";

import type { VnetPeeringExistingItem } from "@/api/settings";

import { classifyPeering, isStalePeering } from "./peeringHealth";

function peering(overrides: Partial<VnetPeeringExistingItem>): VnetPeeringExistingItem {
  return {
    name: "peer-1",
    peering_state: "Connected",
    provisioning_state: "Succeeded",
    remote_vnet: null,
    remote_vnet_exists: null,
    remote_address_prefixes: [],
    allow_virtual_network_access: true,
    allow_forwarded_traffic: false,
    allow_gateway_transit: false,
    use_remote_gateways: false,
    ...overrides,
  };
}

describe("classifyPeering", () => {
  it("flags a confirmed deleted remote VNet as ghost", () => {
    expect(
      classifyPeering(
        peering({ peering_state: "Disconnected", remote_vnet_exists: false }),
      ),
    ).toBe("ghost");
  });

  it("flags a ghost even if the state string is not Disconnected", () => {
    expect(
      classifyPeering(peering({ peering_state: "Updating", remote_vnet_exists: false })),
    ).toBe("ghost");
  });

  it("marks a Disconnected peering with unknown remote as disconnected", () => {
    expect(
      classifyPeering(
        peering({ peering_state: "Disconnected", remote_vnet_exists: null }),
      ),
    ).toBe("disconnected");
  });

  it("treats Connected peerings as healthy", () => {
    expect(classifyPeering(peering({ peering_state: "Connected" }))).toBe("healthy");
  });

  it("treats a Disconnected peering whose remote still exists as healthy", () => {
    expect(
      classifyPeering(
        peering({ peering_state: "Disconnected", remote_vnet_exists: true }),
      ),
    ).toBe("healthy");
  });
});

describe("isStalePeering", () => {
  it("is true for ghost and disconnected, false for healthy", () => {
    expect(
      isStalePeering(peering({ peering_state: "Disconnected", remote_vnet_exists: false })),
    ).toBe(true);
    expect(
      isStalePeering(peering({ peering_state: "Disconnected", remote_vnet_exists: null })),
    ).toBe(true);
    expect(isStalePeering(peering({ peering_state: "Connected" }))).toBe(false);
  });
});
