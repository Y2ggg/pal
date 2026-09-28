// PSM-010; PRD-RELEASE-002; ACC-011/012. All API calls mocked; no production writes.
async page => {
  const check=(value,message)=>{if(!value)throw Error(message);};
  const canonical='skills/example/SKILL.md';
  const makeArtifact=(id,clis)=>({artifact_id:id,profile_id:'skill-md',description:'整理报告',covered_clis:clis,canonical_path:canonical,instructions:'# 用法',markdown_html:'<h1>用法</h1><p>生成<strong>报告</strong>。</p><a href="#" data-doc-link="references/guide.md">查看指南</a>',files:[
    {path:canonical,display_path:'SKILL.md',size:160,sha256:'abc'},
    {path:'skills/example/references/guide.md',display_path:'references/guide.md',size:40,sha256:'def'},
    {path:'skills/example/scripts/read.py',display_path:'scripts/read.py',size:30,sha256:'123'},
    {path:'skills/example/assets/raw.bin',display_path:'assets/raw.bin',size:30,sha256:'456'},
    {path:'skills/example/assets/big.txt',display_path:'assets/big.txt',size:2000000,sha256:'789'},
  ]});
  const doc={unit_id:'example',production:{version_id:'production-one',revision_id:'revision-one',artifacts:[makeArtifact('shared',['claude-code','codex'])]},development:{version_id:null,revision_id:'revision-two',artifacts:[makeArtifact('claude',['claude-code']),makeArtifact('codex',['codex'])]}};
  const status={library:{library_id:'test-library',library_root:'/tmp/example',target_clis:['claude-code','codex']},default_binding:{matches_selected:true},units:[{unit_id:'example',description:'整理报告',description_source:'production',current_revision_id:'revision-two',active_revision_id:'revision-one',active_release_id:'release-one'}],mount:{version_id:'production-one',sync_required:false,recovery_required:false,targets:[]},production:{active_version_id:'production-one',members:[{unit_id:'example',revision_id:'revision-one'}]}};
  const errors=[],posts=[],queries=[];let failure=false,detailFailure=false,slow=false;
  const errorListener=e=>errors.push(e.message); page.on('pageerror',errorListener);
  const route=async route=>{
    const req=route.request();
    if(req.method()!=='GET') {posts.push(req.url());throw Error('Unexpected write');}
    const respond=(body,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
    if(req.url().includes('/api/skill-file?')) {
      const pairs=req.url().split('?')[1].split('&').map(part=>part.split('=').map(decodeURIComponent));const q=Object.fromEntries(pairs);queries.push(q);
      if(failure)return respond({error:'所选版本已变化，请返回集合重新打开详情'},409);
      if(slow && q.path.endsWith('read.py'))await page.waitForTimeout(350);
      if(q.path.endsWith('raw.bin'))return respond({kind:'binary',message:'二进制文件不支持在线预览'});
      if(q.path.endsWith('big.txt'))return respond({kind:'large',message:'文件超过 1 MiB，暂不提供在线预览。'});
      if(q.path.endsWith('read.py'))return respond({kind:'text',content:'print("<script>alert(1)</script>")\nprint("second line")'});
      if(q.path.endsWith('guide.md'))return respond({kind:'markdown',content:'# 指南\n内容',markdown_html:'<h1>指南</h1><p>内容</p><a href="#" data-doc-link="../SKILL.md">返回说明</a><a href="#" data-doc-link="../../../../etc/passwd">越界链接</a><a href="#" data-doc-link="%ZZ">无效链接</a>'});
      return respond({kind:'markdown',content:`---\nname: example\n---\n# ${q.source} ${q.artifact_id}\n`,markdown_html:`<h1>${q.source} ${q.artifact_id}</h1><p>说明正文</p>`});
    }
    if(req.url().includes('/api/skills/'))return detailFailure?respond({error:'详情读取失败'},409):respond(doc);
    if(req.url().endsWith('/api/monitor'))return respond({checked_at:new Date().toISOString(),production_version_id:status.production.active_version_id,mounted_version_id:status.mount.version_id,targets:status.library.target_clis.map(cli_id=>({cli_id,creation:{state:'healthy'},mount:{state:'healthy'}}))});
    return respond(status);
  };
  await page.route('**/api/**',route);
  const open=async()=>{await page.locator('[data-action="skill-details"][data-source="production"]').click();await page.locator('#skill-source').waitFor();};
  const tab=()=>page.getByRole('tab',{name:/文件/});
  const file=name=>page.locator('.file-tree button').filter({hasText:name});
  try {
    await page.setViewportSize({width:1440,height:1000}); await page.reload(); await page.locator('#skill-list').waitFor();
    await page.getByRole('searchbox').fill('报告');await open();
    check(await page.locator('.markdown strong').innerText()==='报告','Overview renders Markdown');
    await page.getByRole('link',{name:'查看指南'}).click();await page.locator('.file-content h1').filter({hasText:'指南'}).waitFor();
    check(await page.locator('.file-folder').count()===3,'Tree shows only actual directories');
    await page.getByRole('link',{name:'返回说明'}).click();await page.locator('.file-content h1').filter({hasText:'production shared'}).waitFor();
    await page.locator('[data-file-mode="source"]').click();
    check((await page.locator('.source-code').innerText()).startsWith('---'),'Source includes full frontmatter');
    check(await page.locator('.source-line').count()>=4,'Source has line rows');
    await page.locator('#skill-source').selectOption('development');await page.locator('.file-content h1').filter({hasText:'development claude'}).waitFor();
    await page.locator('#skill-artifact').selectOption('1');await page.locator('.file-content h1').filter({hasText:'development codex'}).waitFor();
    check(queries.at(-1).revision_id==='revision-two' && queries.at(-1).version_id==='','File requests bind source and revision');
    await file('read.py').click();await page.locator('.source-code').waitFor();
    check(await page.locator('.file-content script').count()===0,'Code never executes HTML');
    await file('raw.bin').click();await page.getByText('二进制文件不支持在线预览').waitFor();
    await file('big.txt').click();await page.getByText('文件超过 1 MiB，暂不提供在线预览。').waitFor();
    failure=true;await file('SKILL.md').click();await page.getByRole('alert').waitFor();
    check(await page.locator('.source-code').count()===0,'Read failure must not retain previous content');
    failure=false;await page.getByRole('button',{name:'重试读取'}).click();await page.locator('.file-content h1').waitFor();
    slow=true;await file('read.py').click();await file('guide.md').click();await page.locator('.file-content h1').filter({hasText:'指南'}).waitFor();
    await page.waitForTimeout(450);check(await page.locator('.file-content h1').innerText()==='指南','Late responses cannot overwrite selection');slow=false;
    const count=queries.length;await page.getByRole('link',{name:'越界链接'}).click();await page.locator('dialog[data-state="failure"]').waitFor();
    check(queries.length===count,'Out-of-scope links never reach file endpoint');await page.keyboard.press('Escape');
    await page.getByRole('link',{name:'无效链接'}).click();await page.locator('dialog[data-state="failure"]').waitFor();await page.keyboard.press('Escape');
    await page.locator('#skill-tab-files').focus();await page.keyboard.press('ArrowLeft');check(await page.locator('#skill-tab-overview').getAttribute('aria-selected')==='true','Tabs support arrow keys');
    await page.getByRole('button',{name:'返回 Skill 集合'}).click();
    check(await page.getByRole('searchbox').inputValue()==='报告','Back preserves search');
    check(await page.locator('[data-action="skill-details"][data-source="production"]').evaluate(el=>el===document.activeElement),'Back restores focus');
    detailFailure=true;await page.locator('[data-action="skill-details"][data-source="production"]').click();await page.getByRole('alert').waitFor();detailFailure=false;await page.getByRole('button',{name:'重新读取详情'}).click();await page.locator('#skill-source').waitFor();
    await page.setViewportSize({width:390,height:844});await tab().click();await page.locator('.file-content h1').waitFor();
    check(await page.evaluate(()=>document.documentElement.scrollWidth===innerWidth),'Mobile file page has no horizontal overflow');
    await page.screenshot({path:'.playwright-mcp/pal-file-browser-mobile.png',fullPage:true,animations:'disabled'});
    await page.setViewportSize({width:1440,height:1000});
    await page.screenshot({path:'.playwright-mcp/pal-file-browser-desktop.png',fullPage:true,animations:'disabled'});
    check(posts.length===0,'Browser is read-only');check(errors.length===0,errors.join('\n'));
    return {passed:['overview and file tree','relative links and traversal guard','source/version/CLI selection','full source and line numbers','binary/large/error/retry','out-of-order reads','keyboard/focus/search preservation','mobile layout'],postRequests:posts,pageErrors:errors};
  } finally { await page.unroute('**/api/**',route);page.off('pageerror',errorListener);await page.setViewportSize({width:1440,height:1000});await page.reload(); }
}
