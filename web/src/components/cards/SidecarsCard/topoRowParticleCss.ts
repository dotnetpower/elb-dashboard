// Animation keyframes — mirror of /sidecar-design-preview. Keep here so the
// card is self-contained; once the preview route is deleted these can move
// into web/src/theme/glass.css.
// The class default omits `animation-iteration-count` so each <RowParticle>
// can override it (we use 1 for event-driven dots).
export const TOPO_ROW_PARTICLE_CSS = `
  @keyframes topoRowParticle {
    0%   { left: 98px;                              opacity: 0; }
    5%   { opacity: 1; }
    95%  { opacity: 1; }
    100% { left: var(--row-end, calc(100% - 12px)); opacity: 0; }
  }
  .topo-row-particle {
    position: absolute;
    top: 50%;
    width: 8px;
    height: 8px;
    margin-top: -4px;
    border-radius: 999px;
    background: var(--accent);
    box-shadow: 0 0 12px 2px rgba(122, 167, 255, 0.55);
    pointer-events: none;
    z-index: 0;
    animation-name: topoRowParticle;
    animation-duration: 1.6s;
    animation-timing-function: linear;
  }
  @media (prefers-reduced-motion: reduce) {
    .topo-row-particle { animation: none; opacity: 0.6; }
  }
`;
