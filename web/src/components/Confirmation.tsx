/**
 * Confirmation placeholder component.
 * T-05 implements the exact-payload WebAuthn approval flow;
 * T-11 adds the full mandate and evidence views.
 * The confirmation fetches the immutable signing envelope
 * directly from the gateway and renders gateway metadata only.
 */
export function Confirmation() {
  return (
    <section className="bg-white rounded-lg shadow p-4">
      <h2 className="text-sm font-medium text-gray-600 mb-2">Confirmation</h2>
      <div className="border rounded p-3 bg-gray-50 text-sm text-gray-500">
        <p>No active transaction to confirm.</p>
        <p className="mt-1 text-xs">
          (Exact-payload WebAuthn confirmation — T-05/T-11)
        </p>
      </div>
    </section>
  )
}
