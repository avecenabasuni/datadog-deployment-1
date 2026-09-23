<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Eminerba instrumentation lab</title>
  <style>body{font:18px system-ui;max-width:800px;margin:4rem auto;padding:1rem}button{padding:.7rem}pre{white-space:pre-wrap;background:#eee;padding:1rem}</style>
</head>
<body>
  <h1>Eminerba instrumentation lab</h1>
  <p>Browser → web Apache proxy → PHP API → MySQL using PDO.</p>
  <p>No RUM SDK is embedded here. Apache auto-injection supplies it after installation.</p>
  <button id="query" type="button">Run read-only SQL request</button>
  <pre id="result">Ready. Click to create application traffic.</pre>
  <p><a href="/health.php">Web HTTP diagnostics</a> · <a href="/api/health.php">API HTTP diagnostics</a></p>
  <script>
    document.getElementById('query').addEventListener('click', async () => {
      const output = document.getElementById('result');
      try {
        const response = await fetch('/api/', {cache: 'no-store', signal: AbortSignal.timeout(15000)});
        if (!response.ok) throw new Error('API returned ' + response.status);
        output.textContent = JSON.stringify(await response.json(), null, 2);
      } catch (error) { output.textContent = String(error); }
    });
  </script>
</body>
</html>
