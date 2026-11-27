/** The Qwen mark. Sized in em, so each context sets its size with font-size. */
export function LogoMark({ className = '' }: { className?: string }) {
  return <img className={`logo-mark ${className}`.trim()} src="/qwen-mark.png" alt="" aria-hidden="true" draggable={false} />
}
