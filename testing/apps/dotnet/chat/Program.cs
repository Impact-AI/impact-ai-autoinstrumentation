// SPDX-License-Identifier: Apache-2.0

// A support assistant on ASP.NET Core that calls a model through Microsoft.Extensions.AI, with no tracing of its own:
//   GET  /healthz        ready
//   POST /chat           the IChatClient the app registers in its services (AddChatClient), as most apps do
//   POST /chat-in-code   an IChatClient the handler builds itself, outside the services
//   POST /agent          a Microsoft Agent Framework agent over the registered IChatClient
//   POST /chat-keyed     an IChatClient registered by type under a key, whose constructor takes the key ([ServiceKey])
// The model is the OpenAI-compatible stub at STUB_URL. Nothing here calls UseOpenTelemetry().
using System.ClientModel;
using Microsoft.Agents.AI;
using Microsoft.Extensions.AI;
using OpenAI;

var port = Environment.GetEnvironmentVariable("PORT") ?? "8000";

IChatClient Model() => Stub.Model();

var builder = WebApplication.CreateBuilder(args);
builder.Services.AddChatClient(_ => Model());
builder.Services.AddKeyedSingleton<IChatClient, SupportChatClient>("support");
var app = builder.Build();

app.MapGet("/healthz", () => "ok");
app.MapPost("/chat", async (IChatClient chat) => (await chat.GetResponseAsync("Where is my order?")).Text);
app.MapPost("/chat-in-code", async () => (await Model().GetResponseAsync("Where is my order?")).Text);
app.MapPost(
    "/chat-keyed",
    async ([FromKeyedServices("support")] IChatClient chat) => (await chat.GetResponseAsync("Where is my order?")).Text);
app.MapPost("/agent", async (IChatClient chat) =>
{
    AIAgent agent = new ChatClientAgent(chat, instructions: "You answer questions about orders.", name: "support");
    return (await agent.RunAsync("Where is my order?")).Text;
});
app.Run($"http://0.0.0.0:{port}");

// A client the container builds, for the key it was registered under.
sealed class SupportChatClient([ServiceKey] string key) : DelegatingChatClient(Stub.Model())
{
    public string Key { get; } = key;
}

static class Stub
{
    public static IChatClient Model() =>
        new OpenAIClient(
                new ApiKeyCredential("stub-key"),
                new OpenAIClientOptions { Endpoint = new Uri($"{Environment.GetEnvironmentVariable("STUB_URL") ?? "http://localhost:8080"}/v1") })
            .GetChatClient("gpt-4o-mini")
            .AsIChatClient();
}
