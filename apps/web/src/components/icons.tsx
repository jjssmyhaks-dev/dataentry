import type { SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement> & { size?: number };

function base({ size = 16, ...props }: IconProps) {
  return {
    width: size,
    height: size,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.7,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    ...props,
  };
}

export const IconGrid = (p: IconProps) => (
  <svg {...base(p)}>
    <rect x="3" y="3" width="7" height="7" rx="1.5" />
    <rect x="14" y="3" width="7" height="7" rx="1.5" />
    <rect x="3" y="14" width="7" height="7" rx="1.5" />
    <rect x="14" y="14" width="7" height="7" rx="1.5" />
  </svg>
);

export const IconInbox = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M3 12h4l2 3h6l2-3h4" />
    <path d="M4.6 5.5h14.8l1.6 6.7V18a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-5.8z" />
  </svg>
);

export const IconLayers = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M12 3 3 7.5 12 12l9-4.5z" />
    <path d="m3 12.5 9 4.5 9-4.5" />
    <path d="m3 17 9 4.5L21 17" />
  </svg>
);

export const IconAlert = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M10.3 3.9 1.9 18a2 2 0 0 0 1.7 3h16.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" />
    <path d="M12 9v4" />
    <path d="M12 17h.01" />
  </svg>
);

export const IconUpload = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
    <path d="m7 9 5-5 5 5" />
    <path d="M12 4v12" />
  </svg>
);

export const IconPulse = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M3 12h4l2.5-7 4 14L16 12h5" />
  </svg>
);

export const IconSliders = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3" />
    <path d="M1 14h6M9 8h6M17 16h6" />
  </svg>
);

export const IconCheck = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="m20 6-11 11-5-5" />
  </svg>
);

export const IconX = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M18 6 6 18M6 6l12 12" />
  </svg>
);

export const IconClock = (p: IconProps) => (
  <svg {...base(p)}>
    <circle cx="12" cy="12" r="9" />
    <path d="M12 7v5l3 2" />
  </svg>
);

export const IconChevron = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="m9 18 6-6-6-6" />
  </svg>
);

export const IconSearch = (p: IconProps) => (
  <svg {...base(p)}>
    <circle cx="11" cy="11" r="7" />
    <path d="m20 20-3.5-3.5" />
  </svg>
);

export const IconLogout = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
    <path d="m16 17 5-5-5-5" />
    <path d="M21 12H9" />
  </svg>
);

export const IconFile = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M14 2H7a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7z" />
    <path d="M14 2v5h5" />
  </svg>
);

export const IconBolt = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M13 2 4.5 13.5H11l-1 8.5L19.5 10H13z" />
  </svg>
);

export const IconShield = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M12 22s8-3.5 8-10V5.5L12 2 4 5.5V12c0 6.5 8 10 8 10Z" />
  </svg>
);

export const IconArrowRight = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M5 12h14M13 6l6 6-6 6" />
  </svg>
);

export const IconTally = (p: IconProps) => (
  <svg {...base(p)}>
    <path d="M4 5h16M9 5v14M15 5v14M4 19h16" />
  </svg>
);
