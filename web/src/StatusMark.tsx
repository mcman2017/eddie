import { COLORS, STATUS_FILL, STATUS_LABEL, type Status } from "./theme";

/** The same mark the map draws for a declaration, so a status never relies on color alone. */
export function StatusMark({ status, size = 14 }: { status: Status; size?: number }) {
  const c = size / 2;
  const r = c - 1.5;
  return (
    <svg className="status-mark" width={size} height={size} viewBox={`0 0 ${size} ${size}`} aria-hidden="true">
      {status === "dead" ? (
        <circle cx={c} cy={c} r={r} fill="none" stroke={STATUS_FILL.dead} strokeWidth={1.25} strokeDasharray="2 2" />
      ) : (
        <circle cx={c} cy={c} r={r} fill={STATUS_FILL[status]} />
      )}
      {status === "verified" && (
        <path
          d={`M ${c - r * 0.5} ${c} L ${c - r * 0.1} ${c + r * 0.42} L ${c + r * 0.55} ${c - r * 0.4}`}
          fill="none"
          stroke={COLORS.ground}
          strokeWidth={Math.max(1.4, size * 0.11)}
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      )}
    </svg>
  );
}

export function StatusChip({ status }: { status: Status }) {
  return (
    <span className={`chip chip-${status}`}>
      <StatusMark status={status} />
      {STATUS_LABEL[status]}
    </span>
  );
}
