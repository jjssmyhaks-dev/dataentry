export type Tone = "ok" | "warn" | "bad";

export interface Span {
  text: string;
  tone: Tone;
  /** The field the reviewer is currently looking at. */
  active: boolean;
  field: string;
}

export interface Segment {
  text: string;
  tone: Tone | null;
  active: boolean;
  field: string | null;
}

/**
 * Find each field's source span in the page text so a reviewer can see where a
 * value came from (Appendix D). Overlaps are resolved in favour of the field the
 * reviewer selected, then the longer span.
 */
export function highlightSpans(pageText: string, spans: Span[]): Segment[] {
  if (!pageText) return [];
  const lower = pageText.toLowerCase();
  type Hit = { start: number; end: number; span: Span };
  const hits: Hit[] = [];

  for (const span of spans) {
    const needle = span.text.trim().toLowerCase();
    if (needle.length < 2) continue;
    let from = 0;
    let guard = 0;
    while (guard < 40) {
      const at = lower.indexOf(needle, from);
      if (at === -1) break;
      hits.push({ start: at, end: at + needle.length, span });
      from = at + needle.length;
      guard += 1;
    }
  }

  hits.sort((a, b) => {
    if (a.span.active !== b.span.active) return a.span.active ? -1 : 1;
    return b.end - b.start - (a.end - a.start);
  });

  const taken: Hit[] = [];
  for (const hit of hits) {
    if (taken.some((t) => hit.start < t.end && hit.end > t.start)) continue;
    taken.push(hit);
  }
  taken.sort((a, b) => a.start - b.start);

  const segments: Segment[] = [];
  let cursor = 0;
  for (const hit of taken) {
    if (hit.start > cursor) {
      segments.push({ text: pageText.slice(cursor, hit.start), tone: null, active: false, field: null });
    }
    segments.push({
      text: pageText.slice(hit.start, hit.end),
      tone: hit.span.tone,
      active: hit.span.active,
      field: hit.span.field,
    });
    cursor = hit.end;
  }
  if (cursor < pageText.length) {
    segments.push({ text: pageText.slice(cursor), tone: null, active: false, field: null });
  }
  return segments;
}
