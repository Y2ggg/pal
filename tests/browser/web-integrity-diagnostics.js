// ACC-012 / MON-002/005. Open the healthy fixture URL with ?damaged=<URL>&source=<URL>.
async page => {
  const urls=await page.evaluate(()=>{const fixture=new URL(location.href);return {healthy:fixture.origin,damaged:fixture.searchParams.get('damaged'),source:fixture.searchParams.get('source')};});
  const errors=[],passed=[];
  const listener=e=>errors.push(e.message);
  page.on('pageerror',listener);
  const button=name=>page.getByRole('button',{name,exact:true});
  try {
    await page.goto(urls.damaged);await page.locator('#skill-list').waitFor();
    await button('库与维护').click();await button('检查状态').click();
    await page.locator('#dialog-title').filter({hasText:'操作未完成'}).waitFor();
    const error=await page.getByRole('dialog').getByRole('alert').innerText();
    if(!error.includes('开发库异常')||!error.includes('asset.bin'))throw Error(error);
    passed.push('真实 HTTP 开发文件损坏明确报错');
    await button('关闭').click();
    if(!(await page.locator('#health').innerText()).includes('异常'))throw Error('failed check stayed green');
    passed.push('失败后页面状态异常');
    await page.goto(urls.healthy);await page.locator('#skill-list').waitFor();
    await button('库与维护').click();await button('检查状态').click();
    await page.locator('#dialog-title').filter({hasText:'检查完成'}).waitFor();
    const success=await page.getByRole('dialog').innerText();
    for(const name of ['开发库通过','生产库通过','CLI 挂载库通过'])if(!success.includes(name))throw Error(name);
    await button('完成').click();passed.push('真实 HTTP 成功显示三层结果');
    await page.goto(urls.source);await page.locator('#health').filter({hasText:'安装有异常'}).waitFor();
    await button('CLI 与系统').click();
    const card=page.locator('[data-monitor-kind="creation"][data-cli="codex"]');
    const source=await card.innerText();
    if(!source.includes('源文件异常')||!source.includes('同版本备份')||await card.locator('button').count())throw Error(source);
    passed.push('源异常分类显示指引且无修复按钮');
    await page.setViewportSize({width:390,height:844});
    if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth))throw Error('mobile overflow');
    passed.push('源异常路径在窄屏内换行');
    if(errors.length)throw Error(errors.join('\n'));
    return {passed,error,success,source,pageErrors:errors,realBusinessWrites:false};
  } finally {
    page.off('pageerror',listener);await page.setViewportSize({width:1440,height:1000});
    await page.goto(urls.healthy);
  }
}
