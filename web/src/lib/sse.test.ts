import { describe, expect, it } from "vitest"

import { parseSseBuffer } from "./sse"

describe("parseSseBuffer", () => {
  it("extracts complete events and keeps the remainder", () => {
    const { events, rest } = parseSseBuffer(
      'data: {"a":1}\n\ndata: {"b":2}\n\ndata: {"partial"',
    )
    expect(events).toEqual(['{"a":1}', '{"b":2}'])
    expect(rest).toBe('data: {"partial"')
  })

  it("joins multi-line data fields", () => {
    const { events } = parseSseBuffer("data: line1\ndata: line2\n\n")
    expect(events).toEqual(["line1\nline2"])
  })

  it("ignores comments and event names", () => {
    const { events } = parseSseBuffer(": keepalive\n\nevent: ping\ndata: x\n\n")
    expect(events).toEqual(["x"])
  })

  it("handles CRLF framing", () => {
    const { events, rest } = parseSseBuffer("data: a\r\n\r\ndata: b")
    expect(events).toEqual(["a"])
    expect(rest).toBe("data: b")
  })

  it("returns everything as rest when no event is complete", () => {
    const { events, rest } = parseSseBuffer("data: half")
    expect(events).toEqual([])
    expect(rest).toBe("data: half")
  })
})
