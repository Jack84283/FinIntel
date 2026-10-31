/** Return true only for the response belonging to the currently expanded row. */
export function isCurrentDetailRequest(currentRequest: number, responseRequest: number): boolean {
  return currentRequest === responseRequest;
}
