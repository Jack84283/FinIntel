/** Guards async model-job details against navigation, replacement and unmount.
 *
 * A response is usable only for the job and request generation that initiated
 * it. No timer or native request is cancelled implicitly; stale results are
 * ignored instead of being rendered under the wrong selection.
 */
export function createModelJobDetailFence() {
  let generation = 0;
  let selectedId: string | null = null;

  return {
    begin(id: string): number {
      selectedId = id;
      return ++generation;
    },
    invalidate(): void {
      selectedId = null;
      generation++;
    },
    isCurrent(id: string, request: number): boolean {
      return id === selectedId && request === generation;
    },
  };
}
