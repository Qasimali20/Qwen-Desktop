import { memo } from 'react'

// Ray lengths (out of 50) give the burst a hand-drawn, slightly irregular look.
const RAYS = [46, 38, 44, 35, 47, 37, 43, 36, 45, 39, 42, 34]

/** A tapered ray from near the centre outwards, with a rounded tip. */
function ray(len: number, angle: number): string {
  const a = (angle * Math.PI) / 180
  const base = 6.2  // half-width at the root
  const tip = 3.2   // radius of the rounded tip
  const r0 = 8
  const px = Math.cos(a + Math.PI / 2)
  const py = Math.sin(a + Math.PI / 2)
  const p = (r: number, w: number) => `${(50 + Math.cos(a) * r + px * w).toFixed(2)} ${(50 + Math.sin(a) * r + py * w).toFixed(2)}`
  return `M${p(r0, base)} L${p(len, tip)} A${tip} ${tip} 0 0 0 ${p(len, -tip)} L${p(r0, -base)} Z`
}

const PATH = RAYS.map((len, i) => ray(len, i * (360 / RAYS.length) - 90 + (i % 2 ? 4 : -3))).join(' ')

function SparkMarkImpl({ size = 36, className }: { size?: number; className?: string }) {
  return (
    <svg className={className} width={size} height={size} viewBox="0 0 100 100" aria-hidden focusable="false">
      <circle cx="50" cy="50" r="11" fill="currentColor" />
      <path d={PATH} fill="currentColor" />
    </svg>
  )
}

export const SparkMark = memo(SparkMarkImpl)
