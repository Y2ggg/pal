// Theme and layout read-only browser checks. Traceability: PSM-007/009; ACC-012.
// Run against an PAL console with at least one committed Skill. No API writes.
async page => {
  const context = await page.context().browser().newContext({colorScheme:'dark',viewport:{width:390,height:844}});
  const errors=[];
  try {
    const test=await context.newPage(); test.on('pageerror',e=>errors.push(e.message));
    await test.goto(page.url()); await test.locator('#skill-list').waitFor();
    if(await test.locator('html').getAttribute('data-theme')!=='dark') throw Error('System theme missing');
    await test.emulateMedia({colorScheme:'light'});
    await test.waitForFunction(()=>document.documentElement.dataset.theme==='light');
    await test.addInitScript(()=>{Storage.prototype.getItem=()=>{throw Error('blocked')};Storage.prototype.setItem=()=>{throw Error('blocked')};});
    await test.reload(); await test.locator('#skill-list').waitFor();
    await test.locator('#theme-toggle').click();
    if(await test.locator('html').getAttribute('data-theme')!=='dark') throw Error('Storage denial blocks toggle');
    await test.getByRole('button',{name:'CLI 与系统',exact:true}).click();
    if(await test.evaluate(()=>document.documentElement.scrollWidth>innerWidth)) throw Error('Mobile sync overflow');
    await test.screenshot({path:'.playwright-mcp/pal-enhanced-history-mobile.png',fullPage:true,animations:'disabled'});
    await test.setViewportSize({width:1440,height:1000});
    await test.screenshot({path:'.playwright-mcp/pal-enhanced-history.png',fullPage:true,animations:'disabled'});
    await test.locator('nav [data-view="collection"]').click();
    const unitId=await test.locator('[data-skill]').first().getAttribute('data-skill');
    await test.locator('[data-action="skill-details"]').first().click();
    await test.getByRole('heading',{name:unitId,exact:true}).waitFor();
    await test.locator('#skill-source').waitFor();
    await test.setViewportSize({width:390,height:844});
    if(await test.evaluate(()=>document.documentElement.scrollWidth>innerWidth)) throw Error('Mobile detail overflow');
    await test.keyboard.press('Escape');
    if(errors.length)throw Error(errors.join('\n'));
    return {passed:['system theme initial and live change','storage-denied manual toggle','mobile sync/detail layout'],pageErrors:errors};
  } finally {await context.close();}
}
