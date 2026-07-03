import { describe, expect, it } from "vitest"

import { detailToMessage } from "./api"

describe("detailToMessage", () => {
  it("passes strings through", () => {
    expect(detailToMessage("boom", "fallback")).toBe("boom")
  })
  it("falls back when detail is missing", () => {
    expect(detailToMessage(null, "Bad Request")).toBe("Bad Request")
  })
  it("flattens FastAPI 422 validation arrays", () => {
    const detail = [
      { loc: ["body", "title"], msg: "field required" },
      { loc: ["body", "budget_usd"], msg: "must be a number" },
    ]
    expect(detailToMessage(detail, "x")).toBe(
      "title: field required; budget_usd: must be a number",
    )
  })
  it("stringifies unknown objects", () => {
    expect(detailToMessage({ a: 1 }, "x")).toBe('{"a":1}')
  })
})
