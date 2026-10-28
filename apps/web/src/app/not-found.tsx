import Link from "next/link";

export default function NotFound() {
  return (
    <div className="grid min-h-screen place-items-center px-6">
      <div className="panel w-full max-w-sm p-7 text-center">
        <h1 className="text-[17px] font-semibold text-ink-100">Not found</h1>
        <p className="mt-1.5 text-[12.5px] leading-relaxed text-ink-400">
          That record does not belong to this tenant, or it has been resolved and removed from the
          queue.
        </p>
        <Link href="/queue" className="btn btn-primary mt-5 w-full">
          Back to the queue
        </Link>
      </div>
    </div>
  );
}
