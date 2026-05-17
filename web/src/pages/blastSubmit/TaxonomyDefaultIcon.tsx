/**
 * Simple inline SVG that represents "taxonomy" — a stylised phylogenetic tree.
 *
 * Used as the fallback artwork inside the Taxonomy modal's image column when
 * the Wikipedia thumbnail endpoint returns no image. Pure-SVG so it scales to
 * any container size without bitmap blur and inherits `currentColor`.
 */

import type { CSSProperties } from "react";

interface Props {
  className?: string;
  style?: CSSProperties;
  ariaLabel?: string;
}

export function TaxonomyDefaultIcon({
  className,
  style,
  ariaLabel = "No taxonomy image — default icon",
}: Props) {
  return (
    <svg
      className={className}
      style={style}
      viewBox="0 0 120 120"
      role="img"
      aria-label={ariaLabel}
      xmlns="http://www.w3.org/2000/svg"
    >
      {/* Subtle backdrop circle (decorative) */}
      <circle
        cx="60"
        cy="60"
        r="52"
        fill="currentColor"
        opacity="0.05"
      />

      {/* Phylogenetic-tree branches */}
      <g
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
        opacity="0.85"
      >
        {/* Root vertical trunk */}
        <path d="M60 86 L60 70" />
        {/* Root horizontal split */}
        <path d="M36 70 L84 70" />
        {/* Left branch */}
        <path d="M36 70 L36 54" />
        <path d="M24 54 L48 54" />
        <path d="M24 54 L24 40" />
        <path d="M48 54 L48 40" />
        {/* Right branch */}
        <path d="M84 70 L84 54" />
        <path d="M72 54 L96 54" />
        <path d="M72 54 L72 40" />
        <path d="M96 54 L96 40" />
      </g>

      {/* Leaf nodes (top tier) */}
      <g fill="currentColor" opacity="0.9">
        <circle cx="24" cy="38" r="4" />
        <circle cx="48" cy="38" r="4" />
        <circle cx="72" cy="38" r="4" />
        <circle cx="96" cy="38" r="4" />
      </g>

      {/* Root node */}
      <circle cx="60" cy="88" r="5" fill="currentColor" opacity="0.7" />
    </svg>
  );
}
