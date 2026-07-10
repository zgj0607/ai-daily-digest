export function parsePositiveInt(value: string | undefined, fallback: number): number {
  const parsed = Number.parseInt(value || '', 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

export async function runWithConcurrency(
  total: number,
  concurrency: number,
  workerFn: (index: number) => Promise<void>
): Promise<void> {
  if (total <= 0) return;
  let nextIndex = 0;
  const workerCount = Math.min(concurrency, total);
  async function worker(): Promise<void> {
    while (true) {
      const currentIndex = nextIndex++;
      if (currentIndex >= total) return;
      await workerFn(currentIndex);
    }
  }
  await Promise.all(Array.from({ length: workerCount }, () => worker()));
}

export function parseJsonResponse<T>(text: string): T {
  let jsonText = text.trim();
  if (jsonText.startsWith('```')) {
    jsonText = jsonText.replace(/^```(?:json)?\n?/, '').replace(/\n?```$/, '');
  }
  return JSON.parse(jsonText) as T;
}
