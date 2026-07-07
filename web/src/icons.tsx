// Icon paths lifted verbatim from design/prototype.html (the I dictionary).
// One <Icon name="..."/> component renders the SVG; sizes via prop.

const PATHS: Record<string, string> = {
  search: '<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  back: '<path d="M15 18l-6-6 6-6"/>',
  dots: '<circle cx="12" cy="5" r="1.4"/><circle cx="12" cy="12" r="1.4"/><circle cx="12" cy="19" r="1.4"/>',
  send: '<path d="M12 19V6M6 12l6-6 6 6"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="2.5"/>',
  bolt: '<path d="M13 2L4 14h7l-1 8 9-12h-7l1-8z"/>',
  queue: '<path d="M4 7h16M4 12h16M4 17h10"/>',
  term: '<path d="M5 8l4 4-4 4M12 16h7"/>',
  close: '<path d="M18 6L6 18M6 6l12 12"/>',
  sun: '<circle cx="12" cy="12" r="4.2"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  moon: '<path d="M20 14.5A8 8 0 019.5 4 8 8 0 1020 14.5z"/>',
  lock: '<rect x="4.5" y="10" width="15" height="10" rx="2.5"/><path d="M8 10V7a4 4 0 018 0v3"/>',
  user: '<circle cx="12" cy="8" r="3.4"/><path d="M5.5 20a6.5 6.5 0 0113 0"/>',
  spark: '<path d="M12 3l1.6 4.9L18.5 9.5l-4.9 1.6L12 16l-1.6-4.9L5.5 9.5l4.9-1.6L12 3z"/>',
  read: '<path d="M5 4h9l5 5v11H5z"/><path d="M14 4v5h5"/>',
  bash: '<rect x="3.5" y="5" width="17" height="14" rx="2.5"/><path d="M7 10l2.5 2L7 14M12.5 14.5H16"/>',
  edit: '<path d="M4 20h4L18.5 9.5a2.1 2.1 0 00-3-3L5 17v3z"/>',
  plan: '<path d="M9 6h11M9 12h11M9 18h11"/><path d="M4.5 6h.01M4.5 12h.01M4.5 18h.01"/>',
  review: '<path d="M2.5 12S6 5 12 5s9.5 7 9.5 7-3.5 7-9.5 7-9.5-7-9.5-7z"/><circle cx="12" cy="12" r="2.7"/>',
  shield: '<path d="M12 3l7 3v5c0 4.5-3 8-7 10-4-2-7-5.5-7-10V6l7-3z"/><path d="M9.2 12l2 2 3.6-4"/>',
  verify: '<circle cx="12" cy="12" r="8.5"/><path d="M8.5 12.5l2.3 2.3 4.7-5"/>',
  run: '<path d="M8 5.5v13l11-6.5-11-6.5z"/>',
  research: '<circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4.5-4.5M11 8v6M8 11h6"/>',
  simplify: '<path d="M4 7h16M7 12h10M10 17h4"/>',
  init: '<path d="M12 3v18M3 12h18" opacity=".25"/><circle cx="12" cy="12" r="8.5"/><path d="M12 8v4l2.5 2.5"/>',
  folder: '<path d="M3.5 7.5A1.5 1.5 0 015 6h4l2 2.5h8A1.5 1.5 0 0120.5 10v8A1.5 1.5 0 0119 19.5H5A1.5 1.5 0 013.5 18z"/>',
  chev: '<path d="M6 9l6 6 6-6"/>',
  cpu: '<rect x="6.5" y="6.5" width="11" height="11" rx="2"/><path d="M9.5 2.5v2M14.5 2.5v2M9.5 19.5v2M14.5 19.5v2M2.5 9.5h2M2.5 14.5h2M19.5 9.5h2M19.5 14.5h2"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7"/>',
  archive: '<rect x="3" y="4" width="18" height="4" rx="1.5"/><path d="M5 8v9a2 2 0 002 2h10a2 2 0 002-2V8"/><path d="M10 12h4"/>',
  copy: '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 012-2h10"/>',
};

export function Icon({ name, size = 20, sw = 1.75 }: { name: string; size?: number; sw?: number }) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} fill="none" stroke="currentColor"
      strokeWidth={sw} strokeLinecap="round" strokeLinejoin="round"
      dangerouslySetInnerHTML={{ __html: PATHS[name] || "" }} />
  );
}

// The Claude mark: a solid terracotta sunburst (radiating tapered rays), in the
// spirit of the official Claude app icon. Filled (not stroked like Icon), so the
// rays read as a solid burst; `color` sets the ray color.
export function ClaudeMark({ size = 16, className }: { size?: number; className?: string }) {
  const N = 12;
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} fill="currentColor" className={className} aria-hidden="true">
      {Array.from({ length: N }, (_, i) => (
        <rect key={i} x="10.95" y="2.5" width="2.1" height="8.1" rx="1.05"
          transform={`rotate(${(360 / N) * i} 12 12)`} />
      ))}
    </svg>
  );
}
