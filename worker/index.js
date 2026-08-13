const SECONDARY_CAPABILITY_STATUS = "unavailable";
const DENIAL = Object.freeze({
  status: 404,
  headers: Object.freeze({ "Cache-Control": "no-store" }),
});

export { SECONDARY_CAPABILITY_STATUS };

export default {
  fetch() {
    return new Response("Not Found", DENIAL);
  },
};
