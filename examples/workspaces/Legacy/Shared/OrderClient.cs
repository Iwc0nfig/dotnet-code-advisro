using System.Net.Http;
using System.Threading.Tasks;

namespace Demo.Shared
{
    public sealed class OrderClient
    {
        public async Task<string> SearchAsync(OrderRequest request)
        {
            using (var client = new HttpClient())
            using (var content = new StringContent("{\"id\":" + request.Id + "}"))
            using (var response = await client.PostAsync("https://api.example.invalid/v1/orders/search", content))
                return await response.Content.ReadAsStringAsync();
        }
    }
}
