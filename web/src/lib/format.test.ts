import { describe, expect, it } from "vitest"

import { ago, kTokens, money, parseKvLines, parseSecretLines } from "./format"

describe("ago", () => {
  const now = 1_750_000_000_000 // fixed ms epoch
  it("formats recent times", () => {
    expect(ago(now / 1000 - 5, now)).toBe("5s ago")
    expect(ago(now / 1000 - 120, now)).toBe("2m ago")
    expect(ago(now / 1000 - 7200, now)).toBe("2h ago")
  })
  it("handles missing timestamps", () => {
    expect(ago(null, now)).toBe("-")
    expect(ago(undefined, now)).toBe("-")
  })
})

describe("money", () => {
  it("formats and tolerates null", () => {
    expect(money(1.23456)).toBe("$1.2346")
    expect(money(2, 2)).toBe("$2.00")
    expect(money(null)).toBe("-")
  })
})

describe("kTokens", () => {
  it("sums and rounds to thousands", () => {
    expect(kTokens(1500, 600)).toBe("2k tok")
    expect(kTokens(null, null)).toBe("0k tok")
  })
})

describe("parseKvLines", () => {
  it("parses KEY=value lines and ignores junk", () => {
    expect(parseKvLines("A=1\n\nB = x=y\nnope\n=bad")).toEqual({
      A: "1",
      B: "x=y",
    })
  })
})

describe("parseSecretLines", () => {
  it("splits env references from stored values", () => {
    const { refs, values } = parseSecretLines(
      "DB_URL=postgres://u:p@h/db\nTOKEN=env:NPM_TOKEN",
    )
    expect(refs).toEqual({ DB_URL: "store", TOKEN: "env:NPM_TOKEN" })
    expect(values).toEqual({ DB_URL: "postgres://u:p@h/db" })
  })
})
