interface Props {
  banner?: string;
  replaying: boolean;
  truncated: boolean;
}

export function ReconnectBanner({ banner, replaying, truncated }: Props) {
  if (!banner && !replaying && !truncated) return null;
  return (
    <div className="banner">
      {replaying && "正在补发历史…"}
      {replaying && banner && " · "}
      {banner}
      {truncated && !replaying && "（部分历史可能缺失）"}
    </div>
  );
}
