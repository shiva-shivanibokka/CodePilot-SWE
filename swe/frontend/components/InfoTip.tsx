"use client";

import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

/**
 * The "?" beside a label.
 *
 * A number on its own invites the wrong reading — is $0.11 the whole benchmark
 * or one issue? is "not resolved" a crash or a real result? These explain the
 * figure beside them without putting a paragraph next to every panel.
 *
 * The bubble is rendered in a portal so it escapes the overflow clipping of the
 * cards it usually sits inside, and it is a real button: reachable by keyboard,
 * dismissible with Escape, and closed by a tap elsewhere on touch.
 */
export default function InfoTip({ text, label }: { text: string; label?: string }) {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);
  const [mounted, setMounted] = useState(false);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const popRef = useRef<HTMLDivElement>(null);
  const id = useId();

  useEffect(() => setMounted(true), []);

  useLayoutEffect(() => {
    if (!open || !buttonRef.current) return;

    const place = () => {
      const anchor = buttonRef.current?.getBoundingClientRect();
      const bubble = popRef.current?.getBoundingClientRect();
      if (!anchor) return;

      const width = bubble?.width ?? 280;
      const height = bubble?.height ?? 80;
      const margin = 12;

      // Prefer above; flip below when there is not room, and keep the bubble
      // inside the viewport horizontally whichever side of the page it is on.
      const above = anchor.top - height - 10;
      const top = above > margin ? above : anchor.bottom + 10;
      const wanted = anchor.left + anchor.width / 2 - width / 2;
      const left = Math.min(Math.max(margin, wanted), window.innerWidth - width - margin);

      setPos({ top, left });
    };

    place();
    window.addEventListener("scroll", place, true);
    window.addEventListener("resize", place);
    return () => {
      window.removeEventListener("scroll", place, true);
      window.removeEventListener("resize", place);
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    const onPointer = (e: PointerEvent) => {
      const target = e.target as Node;
      if (!buttonRef.current?.contains(target) && !popRef.current?.contains(target)) {
        setOpen(false);
      }
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("pointerdown", onPointer);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("pointerdown", onPointer);
    };
  }, [open]);

  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        className="tip"
        data-open={open}
        aria-label={label ? `What is ${label}?` : "More information"}
        aria-expanded={open}
        aria-describedby={open ? id : undefined}
        onPointerEnter={() => setOpen(true)}
        onPointerLeave={(e) => e.pointerType !== "touch" && setOpen(false)}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        onClick={(e) => {
          e.preventDefault();
          setOpen((v) => !v);
        }}
      >
        ?
      </button>

      {mounted && open && pos
        ? createPortal(
            <div
              ref={popRef}
              id={id}
              role="tooltip"
              className="tip-pop"
              style={{ top: pos.top, left: pos.left }}
            >
              {text}
            </div>,
            document.body,
          )
        : null}
    </>
  );
}
