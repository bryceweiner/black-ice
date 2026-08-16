// The mark, inline rather than a PNG: it has to sit on near-black, scale to a
// wall display, and survive a clone without a binary asset.
//
// A shield built out of an ice crystal -- six-fold symmetry, one broken arm,
// because the point of the thing is noticing when something is off.

import React from "react";

export function Mark({ size = 28, className = "" }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 32 32"
      fill="none"
      className={className}
      aria-hidden="true"
      focusable="false"
    >
      <defs>
        <linearGradient id="bi-ice" x1="16" y1="1" x2="16" y2="31" gradientUnits="userSpaceOnUse">
          <stop stopColor="#7dd3fc" />
          <stop offset="1" stopColor="#0284c7" />
        </linearGradient>
      </defs>
      {/* shield */}
      <path
        d="M16 1.5 28 6v9.6c0 6.9-4.7 12.4-12 14.9C8.7 28 4 22.5 4 15.6V6L16 1.5Z"
        stroke="url(#bi-ice)"
        strokeWidth="1.75"
        strokeLinejoin="round"
      />
      {/* crystal */}
      <g stroke="url(#bi-ice)" strokeWidth="1.5" strokeLinecap="round">
        <path d="M16 8v15" />
        <path d="M9.5 11.7 22.5 19.2" />
        <path d="M22.5 11.7 9.5 19.2" />
        <path d="M13.6 10.1 16 11.5l2.4-1.4M13.6 20.9 16 19.5l2.4 1.4" />
      </g>
    </svg>
  );
}

export default function Logo({ size = 28, subtitle }) {
  return (
    <span className="d-inline-flex align-items-center gap-2">
      <Mark size={size} />
      <span className="d-flex flex-column lh-1">
        <span
          className="fw-bold bi-wordmark"
          style={{ letterSpacing: ".18em", fontSize: size * 0.5 }}
        >
          BLACK ICE
        </span>
        {subtitle && (
          <small className="bi-wordmark-sub" style={{ letterSpacing: ".08em", fontSize: size * 0.34 }}>
            {subtitle}
          </small>
        )}
      </span>
    </span>
  );
}
