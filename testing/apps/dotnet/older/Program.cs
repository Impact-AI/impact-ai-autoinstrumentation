// SPDX-License-Identifier: Apache-2.0

// A service on a .NET the kit is not proved on: it starts and answers, with or without the kit.
//   GET  /healthz   ready
//   POST /chat      a fixed answer
var port = Environment.GetEnvironmentVariable("PORT") ?? "8000";
var app = WebApplication.CreateBuilder(args).Build();
app.MapGet("/healthz", () => "ok");
app.MapPost("/chat", () => $"Hello from .NET {Environment.Version.Major}.{Environment.Version.Minor}.");
app.Run($"http://0.0.0.0:{port}");
