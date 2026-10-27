"""Collector browser proxy handling: domestic platforms bypass a system proxy/VPN."""
import pytest
from app import browser_collector as b


@pytest.fixture
def system_proxy(monkeypatch):
    def set_proxies(value):
        import urllib.request
        monkeypatch.setattr(urllib.request, 'getproxies', lambda: value)
    return set_proxies


def test_auto_bypasses_domestic_platforms(monkeypatch, system_proxy):
    monkeypatch.delenv('BROWSER_PROXY', raising=False)
    # what Windows reports for "ProxyServer=127.0.0.1:7890" (Clash / v2rayN system proxy)
    system_proxy({'http': 'http://127.0.0.1:7890', 'https': 'https://127.0.0.1:7890'})
    opts, note = b.proxy_settings()
    assert opts['proxy']['server'] == 'http://127.0.0.1:7890'
    bypass = opts['proxy']['bypass'].split(',')
    for host in ('douyin.com', '.douyin.com', '.xiaohongshu.com', '.bilibili.com', '.zhihu.com', '.weibo.com', '.kuaishou.com'):
        assert host in bypass
    assert 'tiktok.com' not in opts['proxy']['bypass'] and '国内平台直连' in note


def test_https_only_and_socks_proxy_normalised(monkeypatch, system_proxy):
    monkeypatch.delenv('BROWSER_PROXY', raising=False)
    system_proxy({'https': 'https://127.0.0.1:10809'})
    assert b.proxy_settings()[0]['proxy']['server'] == 'http://127.0.0.1:10809'
    system_proxy({'socks': 'socks://127.0.0.1:7891'})
    assert b.proxy_settings()[0]['proxy']['server'] == 'socks5://127.0.0.1:7891'


def test_no_proxy_direct_system_and_explicit(monkeypatch, system_proxy):
    system_proxy({})
    monkeypatch.delenv('BROWSER_PROXY', raising=False)
    assert b.proxy_settings()[0] == {}
    monkeypatch.setenv('BROWSER_PROXY', 'direct')
    assert b.proxy_settings()[0] == {'args': ['--no-proxy-server']}
    monkeypatch.setenv('BROWSER_PROXY', 'system')
    assert b.proxy_settings()[0] == {}
    monkeypatch.setenv('BROWSER_PROXY', 'http://10.0.0.2:8080')
    assert b.proxy_settings()[0]['proxy']['server'] == 'http://10.0.0.2:8080'


def test_launch_passes_proxy_and_keeps_args(monkeypatch, tmp_path, system_proxy):
    monkeypatch.delenv('BROWSER_PROXY', raising=False)
    system_proxy({'http': 'http://127.0.0.1:7890'})
    seen = {}
    class FakeChromium:
        def launch_persistent_context(self, **kw):
            seen.update(kw)
            return 'ctx'
    class FakePW:
        chromium = FakeChromium()
    b._launch(FakePW(), tmp_path, args=['--disable-notifications'])
    assert seen['proxy']['server'] == 'http://127.0.0.1:7890' and seen['args'] == ['--disable-notifications']
    monkeypatch.setenv('BROWSER_PROXY', 'direct')
    seen.clear()
    b._launch(FakePW(), tmp_path, args=['--disable-notifications'])
    assert 'proxy' not in seen and seen['args'] == ['--disable-notifications', '--no-proxy-server']


@pytest.mark.parametrize('raw,expect', [
    ('Page.goto: net::ERR_CONNECTION_CLOSED at https://www.douyin.com/', '规则模式'),
    ('Page.goto: net::ERR_PROXY_CONNECTION_FAILED at https://www.douyin.com/', 'BROWSER_PROXY=direct'),
    ('Page.goto: net::ERR_NAME_NOT_RESOLVED at https://x', '域名解析失败'),
    ('Page.goto: Timeout 45000ms exceeded.', '打开超时'),
])
def test_friendly_network_errors(raw, expect):
    msg = b.friendly_net_error(Exception(raw))
    assert expect in msg
    if 'net::' in raw:
        assert raw.split('net::')[1].split()[0] in msg


def test_network_check_endpoint_reports_and_concludes(context, monkeypatch):
    main, client = context
    from app import netcheck
    monkeypatch.setattr(netcheck, '_resolve', lambda h: (['198.18.0.7'] if 'douyin' in h else ['1.2.3.4'], ''))
    monkeypatch.setattr(netcheck, 'direct_tls', lambda h: (True, '成功 12ms'))
    monkeypatch.setattr(netcheck, 'browser_load', lambda hosts, *a: {h: (False, '连接被中断') for h in hosts})
    monkeypatch.setattr(netcheck, '_system_proxy', lambda: '')
    from app import relay
    monkeypatch.setattr(relay, 'public_resolve', lambda h: ['155.102.54.129'])
    monkeypatch.setattr(netcheck, 'windows_network', lambda: {'adapters': [{'Name': 'Clash', 'InterfaceDescription': 'Meta Tunnel'}], 'dns': []})
    r = client.post('/api/network-check').json()
    assert '虚拟网卡还在运行' in r['conclusion'] and r['rows'][0]['fake_ip'] is True and r['build'] == main.BUILD_ID
    assert r['rows'][0]['public_dns'] == '155.102.54.129' and r['virtual_adapters'] == ['Clash（Meta Tunnel）']
    # after quitting the VPN app: adapter gone, only the cached fake answer is left
    monkeypatch.setattr(netcheck, 'windows_network', lambda: {'adapters': [], 'dns': []})
    assert '清除 DNS 缓存' in client.post('/api/network-check').json()['conclusion']


@pytest.mark.parametrize('direct_ok,proxy_ok,expect', [(True, None, 'BROWSER_PROXY=direct'), (False, True, 'BROWSER_PROXY=system'),
                                                        (False, False, 'Edge')])
def test_network_conclusions(direct_ok, proxy_ok, expect):
    from app import netcheck
    rows = [{'name': '抖音', 'fake_ip': False, 'browser_ok': False, 'direct_ok': direct_ok, 'direct': '',
             'proxy_ok': proxy_ok, 'browser': '连接被中断（ERR_CONNECTION_CLOSED）'}]
    assert expect in netcheck.conclude(rows, 'http://127.0.0.1:7890' if proxy_ok is not None else '')


def test_health_exposes_build_and_root_locally(context):
    main, client = context
    h = client.get('/api/health').json()
    assert h['build'] == main.BUILD_ID and len(h['build']) == 16 and h['root'] == str(main.ROOT)


def test_certificate_interception_conclusion():
    from app import netcheck
    rows = [{'name': '抖音', 'fake_ip': False, 'browser_ok': False, 'direct_ok': True, 'direct': '成功',
             'proxy_ok': None, 'browser': '证书错误：可能是代理/安全软件在拦截 HTTPS（ERR_CERT_AUTHORITY_INVALID）'}]
    assert 'HTTPS 扫描' in netcheck.conclude(rows, '')


class _FakePage:
    def __init__(self, ctx):
        self.ctx = ctx
    def goto(self, url, **kw):
        self.ctx.visits.append((self.ctx.route_key, url))
        host = url.split('/')[2]
        if self.ctx.route_key in self.ctx.broken or (self.ctx.route_key, host) in self.ctx.broken:
            raise Exception(f'Page.goto: net::ERR_CONNECTION_CLOSED at {url}')


class _FakeCtx:
    def __init__(self, route_key, broken, visits):
        self.route_key, self.broken, self.visits, self.pages, self.closed = route_key, broken, visits, [], False
    def new_page(self):
        page = _FakePage(self); self.pages.append(page); return page
    def close(self):
        self.closed = True


def _fake_pw(broken, launched, visits, edge_installed=True):
    class Chromium:
        def launch_persistent_context(self, **kw):
            channel = kw.get('channel')
            if channel == 'msedge' and not edge_installed:
                raise Exception("Chromium distribution 'msedge' is not found")
            if 'proxy' in kw and kw['proxy'].get('bypass') == '<-loopback>':
                key = 'relay'
            elif 'proxy' in kw:
                key = 'auto'
            elif '--no-proxy-server' in kw.get('args', []):
                key = 'direct'
            else:
                key = 'system'
            key = (key, channel)
            launched.append((key, kw['user_data_dir']))
            return _FakeCtx(key, broken, visits)
    class PW:
        chromium = Chromium()
    return PW()


def test_auto_route_switches_when_proxy_breaks_douyin(monkeypatch, tmp_path, system_proxy):
    monkeypatch.delenv('BROWSER_PROXY', raising=False); monkeypatch.delenv('BROWSER_CHANNEL', raising=False)
    monkeypatch.setattr(b, 'system_dns_is_fake', lambda host='': False)
    system_proxy({'http': 'http://127.0.0.1:7890'})
    launched, visits, notes = [], [], []
    profile = tmp_path / 'browser_profile'
    pw = _fake_pw({('auto', None), ('direct', None)}, launched, visits)
    ctx, desc = b._launch_reachable(pw, profile, 'https://www.douyin.com/', note=notes.append)
    assert ctx.route_key == ('system', None) and desc == b.ROUTE_LABELS['system']
    assert [k for k, _ in launched] == [('auto', None), ('direct', None), ('system', None)]
    assert any('已自动切换' in n for n in notes)
    # remembered: next launch goes straight to the working route
    launched.clear()
    ctx2, _ = b._launch_reachable(pw, profile, 'https://www.douyin.com/')
    assert [k for k, _ in launched] == [('system', None)]


def test_route_fallback_never_switches_browser_or_profile(monkeypatch, tmp_path, system_proxy):
    """3.3 bug "登录了又没了": a failing route made collection switch to Edge, whose profile folder
    has no login. Only the proxy handling may change; browser and profile stay the same."""
    monkeypatch.delenv('BROWSER_PROXY', raising=False); monkeypatch.delenv('BROWSER_CHANNEL', raising=False)
    monkeypatch.setattr(b, 'find_browser', lambda c: '')   # no Edge/Chrome: bundled Chromium is chosen
    system_proxy({})
    launched, visits = [], []
    profile = tmp_path / 'browser_profile'
    pw = _fake_pw({('auto', None), ('system', None), ('direct', None), ('relay', None)}, launched, visits)
    b._launch_reachable(pw, profile, 'https://www.douyin.com/')
    assert {k[1] for k, _ in launched} == {None} and {d for _, d in launched} == {str(profile)}


def test_installed_edge_is_chosen_once_and_remembered(monkeypatch, tmp_path):
    monkeypatch.delenv('BROWSER_CHANNEL', raising=False)
    monkeypatch.setattr(b, 'find_browser', lambda c: 'C:/Edge/msedge.exe' if c == 'msedge' else '')
    profile = tmp_path / 'browser_profile'
    assert b.browser_channel(profile) == 'msedge'
    monkeypatch.setattr(b, 'find_browser', lambda c: 'C:/x.exe')  # Chrome installed later: keep Edge
    assert b.browser_channel(profile) == 'msedge'
    monkeypatch.setenv('BROWSER_CHANNEL', 'chromium')
    assert b.browser_channel(profile) is None


def test_all_routes_fail_reports_tun_hint(monkeypatch, tmp_path, system_proxy):
    monkeypatch.delenv('BROWSER_PROXY', raising=False); monkeypatch.delenv('BROWSER_CHANNEL', raising=False)
    system_proxy({'http': 'http://127.0.0.1:7890'})
    notes = []
    broken = {(r, c) for r in ('auto', 'direct', 'system', 'relay') for c in (None, 'msedge')}
    ctx, _ = b._launch_reachable(_fake_pw(broken, [], []), tmp_path / 'p', 'https://www.douyin.com/', note=notes.append)
    assert not ctx.closed and '网络诊断' in notes[-1]


def test_login_wall_is_not_a_network_error(monkeypatch, tmp_path, system_proxy):
    monkeypatch.delenv('BROWSER_PROXY', raising=False)
    system_proxy({})
    launched = []
    ctx, _ = b._launch_reachable(_fake_pw(set(), launched, []), tmp_path / 'p', 'https://www.douyin.com/')
    assert len(launched) == 1


def test_network_check_reports_working_route(monkeypatch):
    from app import netcheck
    rows = [{'name': '抖音', 'fake_ip': False, 'browser_ok': False, 'direct_ok': False, 'direct': '', 'proxy_ok': True, 'browser': ''}]
    msg = netcheck.conclude(rows, 'http://127.0.0.1:7890', [{'route': '全部走系统代理', 'ok': True}])
    assert '自动切换' in msg and '全部走系统代理' in msg


def test_all_fail_without_edge_keeps_last_attempt(monkeypatch, tmp_path, system_proxy):
    monkeypatch.delenv('BROWSER_PROXY', raising=False); monkeypatch.delenv('BROWSER_CHANNEL', raising=False)
    system_proxy({'http': 'http://127.0.0.1:7890'})
    launched, notes = [], []
    broken = {(r, None) for r in ('auto', 'direct', 'system', 'relay')}
    ctx, desc = b._launch_reachable(_fake_pw(broken, launched, [], edge_installed=False), tmp_path / 'p',
                                    'https://www.douyin.com/', note=notes.append)
    # 4 attempts + relaunch of the best one; everything failed, so the first option is kept
    assert len(launched) == 5 and not ctx.closed and desc == b.ROUTE_LABELS['auto'] and '网络诊断' in notes[-1]


def test_fake_dns_puts_direct_channel_first(monkeypatch, tmp_path, system_proxy):
    monkeypatch.delenv('BROWSER_PROXY', raising=False); monkeypatch.delenv('BROWSER_CHANNEL', raising=False)
    system_proxy({'http': 'http://127.0.0.1:7890'})
    monkeypatch.setattr(b, 'system_dns_is_fake', lambda host='': True)
    launched = []
    ctx, desc = b._launch_reachable(_fake_pw(set(), launched, []), tmp_path / 'p', 'https://www.douyin.com/')
    assert launched[0][0] == ('relay', None) and desc == b.ROUTE_LABELS['relay']


def test_relay_proxy_settings_point_to_local_channel():
    opts, note = b.proxy_settings('relay')
    assert opts['proxy']['server'].startswith('http://127.0.0.1:') and '公共 DNS' in note


def _rows(fake=True, public='155.102.54.129'):
    return [{'name': '抖音', 'fake_ip': fake, 'browser_ok': False, 'direct_ok': False, 'direct': '',
             'proxy_ok': None, 'browser': '连接被中断', 'public_dns': public}]


def test_conclusion_tun_adapter_still_running():
    from app import netcheck
    msg = netcheck.conclude(_rows(), '', [], tun=['Meta（Meta Tunnel）'])
    assert '虚拟网卡还在运行' in msg and 'Meta' in msg and '服务模式' in msg


def test_conclusion_dns_server_left_over():
    from app import netcheck
    msg = netcheck.conclude(_rows(), '', [], fake_dns=['以太网: 198.18.0.2'])
    assert '已经关了' in msg and '198.18.0.2' in msg and 'DHCP' in msg


def test_conclusion_dns_cache_left_over_vs_hidden_tun():
    from app import netcheck
    assert '清除 DNS 缓存' in netcheck.conclude(_rows(public='155.102.54.129'), '', [])
    assert '还在工作' in netcheck.conclude(_rows(public='查不到'), '', [])


def test_virtual_adapter_detection():
    from app import netcheck
    net = {'adapters': [{'Name': '以太网', 'InterfaceDescription': 'Realtek PCIe GbE'},
                        {'Name': 'Mihomo', 'InterfaceDescription': 'Meta Tunnel'},
                        {'Name': 'WLAN', 'InterfaceDescription': 'Intel Wi-Fi 6'}],
           'dns': [{'InterfaceAlias': '以太网', 'Servers': '198.18.0.2,223.5.5.5'}, {'InterfaceAlias': 'WLAN', 'Servers': '192.168.1.1'}]}
    assert netcheck.virtual_adapters(net) == ['Mihomo（Meta Tunnel）']
    assert netcheck.fake_dns_servers(net) == ['以太网: 198.18.0.2,223.5.5.5']


def test_public_dns_parser_and_fake_filter(monkeypatch):
    from app import relay
    relay._cache.clear()
    monkeypatch.setattr(relay, '_query', lambda name, server, timeout=2.5: ['198.18.0.9'] if server == '223.5.5.5' else ['1.2.3.4'])
    assert relay.public_resolve('www.douyin.com') == ['1.2.3.4']
    assert relay.is_fake('198.19.255.1') and not relay.is_fake('199.0.0.1')


def test_network_fix_endpoint(context, monkeypatch):
    main, client = context
    from app import netcheck
    monkeypatch.setattr(netcheck, 'flush_dns', lambda: (True, '已成功刷新 DNS 解析缓存。'))
    assert client.post('/api/network-fix').json() == {'ok': True, 'output': '已成功刷新 DNS 解析缓存。'}


def test_route_must_open_every_selected_platform(monkeypatch, tmp_path, system_proxy):
    """The reported bug: xiaohongshu opened, douyin did not, and no other route was tried."""
    monkeypatch.delenv('BROWSER_PROXY', raising=False); monkeypatch.delenv('BROWSER_CHANNEL', raising=False)
    monkeypatch.setattr(b, 'system_dns_is_fake', lambda host='': False)
    system_proxy({'http': 'http://127.0.0.1:7890'})
    launched, visits, notes = [], [], []
    broken = {(('auto', None), 'www.douyin.com'), (('direct', None), 'www.douyin.com')}
    urls = b._test_url_for(['xhs', 'douyin'])
    assert urls == ['https://www.xiaohongshu.com/explore', 'https://www.douyin.com/']
    ctx, desc = b._launch_reachable(_fake_pw(broken, launched, visits), tmp_path / 'p', urls, note=notes.append)
    assert ctx.route_key == ('system', None)
    assert any('www.douyin.com' in n for n in notes)


def test_best_partial_route_is_used_and_reported(monkeypatch, tmp_path, system_proxy):
    monkeypatch.delenv('BROWSER_PROXY', raising=False); monkeypatch.delenv('BROWSER_CHANNEL', raising=False)
    monkeypatch.setattr(b, 'system_dns_is_fake', lambda host='': False)
    system_proxy({'http': 'http://127.0.0.1:7890'})
    launched, notes = [], []
    urls = b._test_url_for(['xhs', 'douyin', 'weibo'])
    broken = {(('auto', None), 'www.douyin.com'), (('auto', None), 'weibo.com'),
              (('direct', None), 'www.douyin.com'),
              ('system', None), ('relay', None)} | {(r, 'msedge') for r in ('auto', 'direct', 'system', 'relay')}
    ctx, desc = b._launch_reachable(_fake_pw(broken, launched, []), tmp_path / 'p', urls, note=notes.append)
    assert ctx.route_key == ('direct', None) and 'www.douyin.com' in notes[-1]
