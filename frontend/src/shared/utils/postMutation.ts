export const POST_MUTATION_REFRESH_FAILED_MESSAGE = "操作已经成功，但页面刷新失败，请手动刷新。";

export async function runPostMutationRefresh(
  refresh: () => boolean | void | Promise<boolean | void>,
): Promise<boolean> {
  try {
    return (await refresh()) !== false;
  } catch {
    return false;
  }
}

export function postMutationMessage(successMessage: string, refreshed: boolean) {
  return refreshed ? successMessage : POST_MUTATION_REFRESH_FAILED_MESSAGE;
}
