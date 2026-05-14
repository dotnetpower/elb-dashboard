export async function getCallerIp(): Promise<string> {
  const response = await fetch("https://api.ipify.org?format=json");
  if (!response.ok) throw new Error("Unable to detect caller IP");
  const data = (await response.json()) as { ip?: string };
  if (!data.ip) throw new Error("Unable to detect caller IP");
  return data.ip;
}