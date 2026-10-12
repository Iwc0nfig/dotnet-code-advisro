namespace Demo.Shared
{
    public class OrderRequest
    {
        public int Id { get; set; }
        public string Search { get; set; }
    }

    public static class OrderService
    {
        public static string Find(int id) { return "Example order " + id; }
    }
}
