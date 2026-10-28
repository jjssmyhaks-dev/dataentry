import type { Metadata } from "next";
import { LandingPage } from "@/components/landing";

export const metadata: Metadata = {
  title: "Autopost — the invoice inbox that posts itself",
  description:
    "Autopost reads supplier invoices, purchase orders and bank statements, checks every GST rule, and posts high-confidence records into TallyPrime. Only the doubtful ones reach a person.",
  openGraph: {
    title: "Autopost — the invoice inbox that posts itself",
    description: "Autonomous data entry for Indian manufacturers: GST-validated records posted to TallyPrime.",
    type: "website",
  },
};

export default function Home() {
  return <LandingPage />;
}
