using Demo.Shared;

var builder = WebApplication.CreateBuilder(args);
var app = builder.Build();
app.MapPost("/v1/orders/search", (OrderRequest request) => OrderService.Find(request.Id));
app.Run();
