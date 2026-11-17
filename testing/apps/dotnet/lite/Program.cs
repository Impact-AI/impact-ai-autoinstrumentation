// SPDX-License-Identifier: Apache-2.0

// A support assistant that registers its IChatClient with Microsoft.Extensions.AI's abstractions and the OpenAI
// provider's package alone (Microsoft.Extensions.AI.OpenAI needs no more), so it has no Microsoft.Extensions.AI, where
// UseOpenTelemetry() is:
//   GET  /healthz   ready
//   POST /chat      the IChatClient the app registers in its services
// The model is the OpenAI-compatible stub at STUB_URL.
using System.ClientModel;
using Microsoft.Extensions.AI;
using OpenAI;

var stub = Environment.GetEnvironmentVariable("STUB_URL") ?? "http://localhost:8080";
var port = Environment.GetEnvironmentVariable("PORT") ?? "8000";

var builder = WebApplication.CreateBuilder(args);
builder.Services.AddSingleton<IChatClient>(
    _ => new OpenAIClient(new ApiKeyCredential("stub-key"), new OpenAIClientOptions { Endpoint = new Uri($"{stub}/v1") })
        .GetChatClient("gpt-4o-mini")
        .AsIChatClient());
var app = builder.Build();

app.MapGet("/healthz", () => "ok");
app.MapPost("/chat", async (IChatClient chat) => (await chat.GetResponseAsync("Where is my order?")).Text);
app.Run($"http://0.0.0.0:{port}");
