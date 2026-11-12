/** Renders /penny.png, falling back to a CSS coin with a "P" when the image is missing. */
import { useState } from "react";

export default function PennyAvatar({ size }: { size: number }) {
  const [failed, setFailed] = useState(false);
  if (failed) {
    return <span className="coin" style={{ width: size, height: size, fontSize: size * 0.5 }}>P</span>;
  }
  return <img src="/penny.png" alt="Penny" width={size} height={size} style={{ borderRadius: "50%", display: "block" }} onError={() => setFailed(true)} />;
}
