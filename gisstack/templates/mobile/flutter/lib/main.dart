import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_map/flutter_map.dart';
import 'package:latlong2/latlong.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'config.dart';

final Map<String, dynamic> cfg = jsonDecode(kMapConfigJson) as Map<String, dynamic>;

const Map<String, Map<String, String>> tr = {
  'ka': {'layers': 'ფენები', 'reset': 'საწყისი ხედი', 'lang': 'EN'},
  'en': {'layers': 'Layers', 'reset': 'Reset view', 'lang': 'ქართ'},
};

String absUrl(String u) {
  final full = u.startsWith('http') ? u : kServer + u;
  return full.replaceAll('{r}', '');
}

void main() => runApp(const MapApp());

class MapApp extends StatelessWidget {
  const MapApp({super.key});
  @override
  Widget build(BuildContext context) => MaterialApp(
        debugShowCheckedModeBanner: false,
        theme: ThemeData(colorSchemeSeed: const Color(0xFF14385C), useMaterial3: true),
        home: const MapPage(),
      );
}

class MapPage extends StatefulWidget {
  const MapPage({super.key});
  @override
  State<MapPage> createState() => _MapPageState();
}

class _MapPageState extends State<MapPage> {
  final MapController controller = MapController();
  late String lang = (cfg['lang'] as String?) ?? 'ka';
  late final List<dynamic> layers = cfg['layers'] as List<dynamic>;
  late final Map<String, bool> enabled = {
    for (var i = 0; i < layers.length; i++) (layers[i] as Map)['id'] as String: i == 0,
  };

  @override
  void initState() {
    super.initState();
    SharedPreferences.getInstance().then((p) {
      final saved = p.getString('lang');
      if (saved != null && tr.containsKey(saved) && mounted) setState(() => lang = saved);
    });
  }

  Future<void> toggleLang() async {
    final next = lang == 'ka' ? 'en' : 'ka';
    setState(() => lang = next);
    (await SharedPreferences.getInstance()).setString('lang', next);
  }

  LatLng get center => LatLng((cfg['center'] as List)[0] as double, (cfg['center'] as List)[1] as double);

  List<Widget> buildTiles() {
    final out = <Widget>[];
    final base = cfg['basemap'] as Map<String, dynamic>?;
    if (base != null) {
      final subs = (base['subdomains'] as String?) ?? '';
      out.add(TileLayer(
        urlTemplate: absUrl(base['url'] as String),
        subdomains: subs.isEmpty ? const ['a', 'b', 'c'] : subs.split(''),
        userAgentPackageName: kApplicationId,
        maxNativeZoom: 19,
      ));
    }
    for (final l in layers.cast<Map<String, dynamic>>()) {
      if (enabled[l['id']] != true) continue;
      if (l['type'] == 'wms') {
        out.add(TileLayer(
          wmsOptions: WMSTileLayerOptions(
            baseUrl: '$kServer${l['url']}?',
            layers: [l['layers'] as String],
            format: 'image/png',
            transparent: true,
          ),
          userAgentPackageName: kApplicationId,
        ));
      } else {
        out.add(TileLayer(
          urlTemplate: absUrl(l['url'] as String),
          userAgentPackageName: kApplicationId,
          maxNativeZoom: 19,
        ));
      }
    }
    return out;
  }

  @override
  Widget build(BuildContext context) {
    final t = tr[lang]!;
    final title = (cfg['title'] as Map)[lang] as String;
    return Scaffold(
      appBar: AppBar(
        title: Text(title),
        actions: [
          IconButton(
            tooltip: t['reset'],
            icon: const Icon(Icons.home_outlined),
            onPressed: () => controller.move(center, (cfg['zoom'] as num).toDouble()),
          ),
          TextButton(onPressed: toggleLang, child: Text(t['lang']!)),
        ],
      ),
      drawer: Drawer(
        child: SafeArea(
          child: ListView(children: [
            ListTile(title: Text(t['layers']!, style: Theme.of(context).textTheme.titleMedium)),
            for (final l in layers.cast<Map<String, dynamic>>())
              SwitchListTile(
                title: Text(((l['title'] as Map)[lang] ?? (l['title'] as Map)['en']) as String),
                value: enabled[l['id']] == true,
                onChanged: (v) => setState(() => enabled[l['id'] as String] = v),
              ),
          ]),
        ),
      ),
      body: FlutterMap(
        mapController: controller,
        options: MapOptions(initialCenter: center, initialZoom: (cfg['zoom'] as num).toDouble()),
        children: [
          ...buildTiles(),
          RichAttributionWidget(attributions: [
            TextSourceAttribution((cfg['basemap'] as Map?)?['attr']?.toString().replaceAll('&copy;', '©') ?? ''),
          ]),
        ],
      ),
    );
  }
}
