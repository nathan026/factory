import { chromium } from "playwright-core";

const [, , url, id, output] = process.argv;
const browser = await chromium.launch();
try {
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await page.goto(url + "/?task=" + id);
  await page.getByLabel("Local sign-in token").fill(process.env.FACTORY_LOCAL_TOKEN);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.getByRole("button", { name: "Evidence", exact: true }).click();
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download task evidence" }).click();
  await (await download).saveAs(output);
} finally {
  await browser.close();
}
