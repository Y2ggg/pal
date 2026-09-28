// MON-001..006; all writes intercepted, no real installation changes.
async page => {
  const check=(value,message)=>{if(!value)throw Error(message);};
  const status={library:{library_id:'monitor-test',library_root:'/tmp/monitor-test',target_clis:['claude-code','codex']},default_binding:{matches_selected:true},units:[{unit_id:'example',current_revision_id:'new',development_revision_id:'new',active_revision_id:'old',production_revision_id:'old',mounted_revision_id:'old',description:'测试 Skill'}],mount:{version_id:'production-one',sync_required:false,recovery_required:false,targets:[]},production:{active_version_id:'production-one',members:[{unit_id:'example',revision_id:'old'}]}};
  const make=()=>({checked_at:new Date().toISOString(),production_version_id:'production-one',mounted_version_id:'production-one',targets:['claude-code','codex'].map(cli_id=>({cli_id,creation:{state:'outdated',repairable:true,expected_version:'0.2.0+native.7',installed_version:'0.1.0+native.6',message:'入口需要更新'},mount:cli_id==='codex'?{state:'missing',repairable:true,message:'插件缺装'}:{state:'healthy',message:'安装校验通过'}}))});
  let report=make(), fail=false;const posts=[],errors=[];
  const onError=e=>errors.push(e.message);page.on('pageerror',onError);
  const route=async route=>{
    const req=route.request(),path=req.url().split('/').slice(3).join('/').split('?')[0].replace(/^/,'/');
    const respond=(body,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
    if(path==='/api/monitor')return fail?respond({error:'官方 CLI 检查超时'},409):respond(report);
    if(req.method()==='GET')return respond(status);
    const body=req.postDataJSON();posts.push({path,body});
    if(path==='/api/installations/repair') {
      check(Object.keys(body).sort().join(',')==='cli_id,expected_version,kind','Repair scope must be explicit');
      report.targets.find(t=>t.cli_id===body.cli_id)[body.kind]={state:'healthy',message:'校验通过'};
      return respond({monitor:report,message:'修复并核验完成'});
    }
    if(path==='/api/doctor')return respond(fail?{status,message:'安装检查失败'}:{status,monitor:report,message:'检查完成'});
    throw Error(`Unexpected API write ${path}`);
  };
  await page.route('**/api/**',route);
  const button=name=>page.getByRole('button',{name,exact:true});
  try {
    await page.reload();await page.locator('#skill-list').waitFor();
    await page.locator('#health').filter({hasText:'安装有异常'}).waitFor();
    await page.locator('#health').click();
    await page.getByRole('heading',{name:'系统创建入口',exact:true}).waitFor();
    check((await page.locator('[data-monitor-kind="creation"][data-cli="codex"]').innerText()).includes('native.6'),'Installed version visible');
    const upgrade=page.locator('[data-monitor-kind="creation"][data-cli="codex"] button');
    await upgrade.click();await button('取消').click();check(posts.length===0,'Cancel must not write');
    await upgrade.click();await button('确认修复').click();
    await page.locator('#dialog-title').filter({hasText:'修复并核验完成'}).waitFor();await button('完成').click();
    check(posts.length===1&&posts[0].body.kind==='creation'&&posts[0].body.expected_version==='0.2.0+native.7','System upgrade must not sync business content');
    await page.locator('[data-monitor-kind="mount"][data-cli="codex"] button').click();await button('确认修复').click();
    await page.locator('#dialog-title').filter({hasText:'修复并核验完成'}).waitFor();await button('完成').click();
    check(posts[1].body.kind==='mount'&&posts[1].body.expected_version==='production-one','Mount repair binds last synced content');
    await page.locator('nav [data-view="collection"]').click();check((await page.locator('[data-skill="example"]').innerText()).includes('开发有更新'),'Repairs preserve unpublished content');
    fail=true;await page.locator('nav [data-view="sync"]').click();await button('重新检查安装').click();await page.locator('#dialog-title').filter({hasText:'检查完成 · 有项目需处理'}).waitFor();await button('完成').click();
    check((await page.locator('#health').innerText()).includes('安装检查失败'),'Read failure cannot stay green');
    await page.locator('#health').click();check(await page.locator('[data-action="repair-installation"]').count()===0,'Unknown inventory cannot offer blind repair');
    await page.setViewportSize({width:390,height:844});
    check(!await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),'Mobile monitoring layout');
    if(errors.length)throw Error(errors.join('\n'));
    return {passed:['per-target outdated and missing display','cancel and independent system upgrade','repair mounted content without sync','preserved unpublished content','unknown not healthy','mobile layout'],posts,pageErrors:errors};
  } finally {await page.unroute('**/api/**',route);page.off('pageerror',onError);}
}
